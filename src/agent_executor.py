"""
Потокобезопасный исполнитель агента.

Один мьютекс на экземпляр, держится кратко (доступ к state / pending / stop).
State создаётся заново при каждом run(). Возвращённый из get_state() state
нельзя мутировать снаружи; изменения — только через add_message() или под get_lock().
"""

import os
import json
import threading
from typing import Any

from processing_responses import run_agent
from run_context import RunContext
from summary_pipeline import maybe_run_summary_compaction
from states import State, save_state, dump_storage
from thread_store import (
    ThreadSession,
    reset_thread_files,
    resolve_threads_dir,
)
from tools.skills.registry import get_skills_registry
from user_message_policy import (
    TECHNICAL_TYPE_CURRENT_CONTEXT,
    is_dialog_summary_user_message_text,
    wrap_technical_user_content,
)
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func
from utils.local_render_system_prompt import render_system_prompt


class AgentExecutor:
    """
    Потокобезопасный запуск агента с возможностью добавлять сообщения во время run
    и останавливать выполнение через stop_run().
    """

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx
        self._lock = threading.Lock()
        self._pending_messages: list[dict] = []
        self._state: State | None = None

    def run(
        self,
        message: str,
        *,
        thread_id: str | None = None,
        start_new_thread: bool = False,
        system: str | None = None,
        verbose: bool | None = None,
    ) -> Any:
        """Запускает агента. При каждом вызове state создаётся заново."""
        config_dict = self._ctx.config_dict
        logger = getattr(self._ctx, "logger", None)
        effective_verbose = (
            verbose if verbose is not None else config_dict.get("verbose", False)
        )
        skills_list = get_skills_registry(config_dict).list_skills()

        thread_session: ThreadSession | None = None
        thread_notes_md: str | None = None
        previous_response_id: str | None = None
        backend_api_mode = str(config_dict.get("backend_api_mode", "responses")).strip().lower()
        if thread_id is not None:
            system_id = system or "cli"
            if start_new_thread:
                threads_dir = resolve_threads_dir(config_dict)
                reset_thread_files(threads_dir, system_id=system_id, thread_id=thread_id)
            thread_session = ThreadSession.from_config(
                config_dict=config_dict, system_id=system_id, thread_id=thread_id
            )
            if backend_api_mode == "responses":
                previous_response_id = thread_session.previous_response_id
            meta = getattr(thread_session, "meta", None) or {}
            maybe_notes = meta.get("notes_md")
            thread_notes_md = (
                maybe_notes if isinstance(maybe_notes, str) else None
            )
        else:
            system_id = system or "cli"
        is_cli_mode = system_id == "cli"
        summary_params = (
            config_dict.get("summary_params")
            if isinstance(config_dict.get("summary_params"), dict)
            else {}
        )
        summary_trigger_raw = summary_params.get(
            "chat_completions_summary_trigger_chars", 0
        )
        try:
            summary_trigger_chars = int(summary_trigger_raw)
        except (TypeError, ValueError):
            summary_trigger_chars = 0

        chat_completions_system_prompt: str | None = None
        rendered_system_prompt: str | None = None
        if backend_api_mode == "chat_completions":
            prompt_path = config_dict.get("chat_completions_system_prompt_path")
            if isinstance(prompt_path, str) and prompt_path.strip():
                try:
                    with open(prompt_path, "r", encoding="utf-8") as fd:
                        chat_completions_system_prompt = fd.read().strip()
                except OSError as e:
                    if effective_verbose:
                        print(
                            f"Не удалось прочитать chat_completions system prompt: {prompt_path}"
                        )
                    raise e

        state_data = {
            "limit_project_metadata": config_dict.get("limit_project_metadata", False),
            "parent_state_id": None,
            "verbose": effective_verbose,
            "model": config_dict["model"],
            "tools_spec": self._ctx.tools_spec,
            "tools_descr": self._ctx.tools_description,
            "max_steps": config_dict["max_steps"],
            "skills_list": skills_list,
            "system_prompt_id": config_dict["system_prompt_id"],
            "chat_completions_system_prompt": chat_completions_system_prompt,
            "use_server_subagents": config_dict.get("use_server_subagents", False),
            "agent_md_path": config_dict.get("agent_md_path"),
            "thread_id": thread_id,
            "notes_md": thread_notes_md,
        }
        state = State(**state_data)
        state.backend_api_mode = backend_api_mode
        state.dialog_steps_offset = 0
        if thread_session is not None:
            meta = getattr(thread_session, "meta", None) or {}
            state.init_user_messages_log(meta.get("user_messages_log"))
            state.dialog_steps_offset = int(meta.get("dialog_steps_offset", 0) or 0)
        if (
            backend_api_mode == "chat_completions"
            and isinstance(chat_completions_system_prompt, str)
            and chat_completions_system_prompt.strip()
        ):
            # Для chat/completions backend'а system prompt должен быть в истории сообщений.
            # Делаем это на уровне state-сессии, чтобы transport/formatting не знал
            # про источники/файлы системного промпта.
            rendered_system_prompt = render_system_prompt(
                system_prompt=chat_completions_system_prompt.strip(),
                tools_spec=state.tools,
                project_info=state.initial_project_data,
                specific_prompt=system,
            )
            state.messages = [
                {
                    "role": "system",
                    "content": rendered_system_prompt,
                    "id": generate_message_id_func("m_sys"),
                }
            ]
        save_state(state)

        with self._lock:
            self._state = state
            self._pending_messages = []

        def _resolve_summary_trace_path() -> str:
            path = getattr(self._ctx, "summary_trace_path", "")
            if isinstance(path, str) and path.strip():
                return path.strip()
            cfg = getattr(self._ctx, "config_dict", {}) or {}
            if not cfg.get("save_logs", False):
                return ""
            base_dir = cfg.get("logs_dump_dir") or os.getcwd()
            return os.path.join(base_dir, "summary_trace.jsonl")

        def _append_summary_trace(
            *, trigger_reason: str, trace_data: dict, newly_summarized_message_id: str
        ) -> None:
            trace_path = _resolve_summary_trace_path()
            if not trace_path:
                return
            trace_dir = os.path.dirname(trace_path)
            if trace_dir:
                os.makedirs(trace_dir, exist_ok=True)
            payload = {
                "trigger_reason": trigger_reason,
                "summary_trace": trace_data or {},
                "newly_summarized_message_id": newly_summarized_message_id,
            }
            with open(trace_path, "a", encoding="utf-8") as fd:
                fd.write(json.dumps(payload, ensure_ascii=False))
                fd.write("\n")

        def _contains_dialog_summary_message(messages_list: list[dict]) -> bool:
            for msg in messages_list:
                if not isinstance(msg, dict):
                    continue
                if str(msg.get("role", "")).strip().lower() != "user":
                    continue
                content = msg.get("content")
                if not isinstance(content, str):
                    continue
                if is_dialog_summary_user_message_text(content):
                    return True
            return False

        def _handle_summary_applied(event: dict[str, Any]) -> None:
            if thread_session is not None:
                thread_session.archive_and_compact_summary_payload(
                    event.get("summary_payload", {})
                )
            _append_summary_trace(
                trigger_reason=str(event.get("trigger_reason") or ""),
                trace_data=event.get("summary_trace", {}),
                newly_summarized_message_id=str(
                    event.get("newly_summarized_message_id") or ""
                ),
            )

        if backend_api_mode == "chat_completions" and summary_trigger_chars > 0:
            try:
                maybe_run_summary_compaction(
                    ctx=self._ctx,
                    state=state,
                    summary_trigger_chars=summary_trigger_chars,
                    trigger_reason="pre_run",
                    is_cli_mode=is_cli_mode,
                    on_summary_applied=_handle_summary_applied,
                )
            except Exception as summarize_exc:
                if logger is not None:
                    if thread_session is not None:
                        logger.warning(
                            "summary_failed reason=pre_run thread_id=%s error=%s",
                            thread_session.thread_id,
                            summarize_exc,
                            exc_info=True,
                        )
                    else:
                        logger.warning(
                            "summary_failed reason=pre_run error=%s",
                            summarize_exc,
                            exc_info=True,
                        )
                if is_cli_mode:
                    print(
                        "[summary] Ошибка суммаризации: "
                        f"error={summarize_exc}"
                    )

        new_user_message = {
            "role": "user",
            "content": message,
            "id": generate_message_id_func("m"),
        }
        technical_user_message = None
        if (
            thread_session is not None
            and isinstance(thread_notes_md, str)
            and thread_notes_md.strip()
            and not _contains_dialog_summary_message(thread_session.history_messages)
        ):
            # Первый user-сообщение в такте: "техническая" инструкция серверу/модели,
            # что клиент подгружает thread notes и их нужно прочитать tool'ом.
            technical_user_message = {
                "role": "user",
                "content": wrap_technical_user_content(
                    (
                        'client has non-empty thread notes; call `thread_notes` '
                        'with action="read" to load them.'
                    ),
                    technical_type=TECHNICAL_TYPE_CURRENT_CONTEXT,
                ),
                "id": generate_message_id_func("m"),
            }
            if logger is not None:
                logger.info(
                    "summary_context_message_added thread_id=%s reason=notes_present_no_dialog_summary",
                    thread_session.thread_id,
                )
        elif (
            thread_session is not None
            and isinstance(thread_notes_md, str)
            and thread_notes_md.strip()
            and _contains_dialog_summary_message(thread_session.history_messages)
            and logger is not None
        ):
            logger.info(
                "summary_context_message_skipped thread_id=%s reason=dialog_summary_already_present",
                thread_session.thread_id,
            )

        messages = [new_user_message]

        persist_messages_callback = None
        get_full_history_messages = None
        if thread_session is not None:
            state.previous_response_id = previous_response_id
            thread_session.set_runtime_messages(state.messages)

            base_messages: list[dict] = (
                thread_session.history_messages
                if backend_api_mode == "chat_completions" or previous_response_id is None
                else []
            )
            # При продолжении через `-t` в chat/completions mode мы отправляем
            # бэкенду всю историю треда. Чтобы локальные лимиты `max_steps`
            # не "сдвигались" из-за старых сообщений, будем пропускать инкремент
            # счётчика шагов на первом `user_ask`-цикле, если история непустая.
            state.skip_initial_user_ask_step_when_history = len(base_messages) > 0
            messages = [*base_messages]
            if technical_user_message is not None:
                messages.append(technical_user_message)
            messages.append(new_user_message)

            def _persist_thread_batch(step_messages: list[dict]) -> None:
                # Persist notes from in-memory state into thread meta.
                # Tools mutate state, but ThreadSession writes self.meta to disk.
                if (
                    backend_api_mode == "chat_completions"
                    and isinstance(rendered_system_prompt, str)
                    and rendered_system_prompt.strip()
                ):
                    thread_session.meta[
                        "chat_completions_system_prompt_rendered"
                    ] = rendered_system_prompt
                notes_md = getattr(state, "notes_md", None)
                if isinstance(notes_md, str):
                    thread_session.meta["notes_md"] = notes_md
                state.append_user_messages_to_log(step_messages)
                thread_session.meta["user_messages_log"] = state.user_messages_log
                thread_session.meta["backend_api_mode"] = state.backend_api_mode
                thread_session.backend_api_mode = state.backend_api_mode
                last_backend_response_id = getattr(
                    state, "last_backend_response_id", None
                )
                if isinstance(last_backend_response_id, str) and last_backend_response_id:
                    thread_session.meta["last_backend_response_id"] = (
                        last_backend_response_id
                    )
                thread_session.persist_batch(
                    step_messages, previous_response_id=state.previous_response_id
                )

            persist_messages_callback = _persist_thread_batch
            get_full_history_messages = thread_session.get_full_history_messages
        elif backend_api_mode == "chat_completions" and summary_trigger_chars > 0:
            def _persist_runtime_batch(step_messages: list[dict]) -> None:
                state.append_user_messages_to_log(step_messages)

            persist_messages_callback = _persist_runtime_batch

        def _post_step_summary_hook() -> None:
            if (
                state.backend_api_mode != "chat_completions"
                or summary_trigger_chars <= 0
            ):
                return
            try:
                maybe_run_summary_compaction(
                    ctx=self._ctx,
                    state=state,
                    summary_trigger_chars=summary_trigger_chars,
                    trigger_reason="post_step",
                    is_cli_mode=is_cli_mode,
                    on_summary_applied=_handle_summary_applied,
                )
            except Exception as summarize_exc:
                if logger is not None:
                    if thread_session is not None:
                        logger.warning(
                            "summary_failed reason=post_step thread_id=%s error=%s",
                            thread_session.thread_id,
                            summarize_exc,
                            exc_info=True,
                        )
                    else:
                        logger.warning(
                            "summary_failed reason=post_step error=%s",
                            summarize_exc,
                            exc_info=True,
                        )
                if is_cli_mode:
                    if thread_session is not None:
                        print(
                            "[summary] Ошибка суммаризации: "
                            f"thread={thread_session.thread_id}, error={summarize_exc}"
                        )
                    else:
                        print(
                            "[summary] Ошибка суммаризации: "
                            f"error={summarize_exc}"
                        )

        def get_pending_messages() -> list[dict]:
            with self._lock:
                out = self._pending_messages[:]
                self._pending_messages.clear()
                return out

        def check_stop() -> bool:
            with self._lock:
                return self._state.stop_requested if self._state is not None else False

        response = run_agent(
            messages,
            state,
            self._ctx,
            get_full_history_messages=get_full_history_messages,
            persist_messages_callback=persist_messages_callback,
            post_step_hook=_post_step_summary_hook,
            get_pending_messages=get_pending_messages,
            check_stop=check_stop,
        )

        if config_dict.get("dump_storage"):
            cur_dir = config_dict.get("logs_dump_dir") or os.getcwd()
            dump_storage(cur_dir, indent=2)

        if config_dict.get("console_trace", True):
            print(f"\nПуть к файлу с логами: {self._ctx.logging_path}\n")
        return response

    def add_message(self, message: dict) -> None:
        """Потокобезопасно добавляет сообщение в очередь на обработку агентом."""
        with self._lock:
            self._pending_messages.append(message)

    def get_state(self) -> State | None:
        """
        Возвращает текущий state (под замком).
        Не мутировать снаружи; изменения — только через add_message() или под get_lock().
        """
        with self._lock:
            return self._state

    def get_lock(self) -> threading.Lock:
        """
        Возвращает мьютекс executor'а.
        Изменения state — только под этим замком.
        """
        return self._lock

    def stop_run(self) -> None:
        """Выставляет флаг остановки; агент завершит текущую итерацию и выйдет."""
        with self._lock:
            if self._state is not None:
                self._state.stop_requested = True
