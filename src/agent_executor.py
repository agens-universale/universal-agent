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
from summarize.compaction import (
    history_exceeds_summary_trigger,
    run_summary_compaction,
)
from summarize.config import parse_summary_trigger_chars
from summarize.summary_serialization import history_has_dialog_summary_message
from states import State, SharedStateData, save_state, dump_storage
from thread_store import (
    ThreadSession,
    reset_thread_files,
    resolve_threads_dir,
)
from tools.connect_mcp.persistent_stdio import PersistentStdioRegistry
from tools.connect_mcp.stream_client import _SESSION_MANAGER as _STREAM_SESSION_MANAGER
from tools.skills.registry import get_skills_registry
from user_message_policy import (
    TECHNICAL_TYPE_CURRENT_CONTEXT,
    wrap_technical_user_content,
)
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func
from utils.local_render_system_prompt import render_system_prompt

ORCHESTRATOR_SPECIFIC_ROLE = (
    "You are an orchestrator agent. Your job is to delegate tasks to subagents and validate their results.\n"
    "- Decompose complex user requests into logical subtasks and delegate appropriately.\n"
    "- Favor planning and delegation over direct execution when specialized agents can improve quality or efficiency.\n"
    "At the beginning of a task, if needed, prefer using the planner subagent to build an execution plan. "
    "After delegating a task to a subagent, you must validate the result either yourself or by delegating "
    "a dedicated review to the critic subagent.\n"
    
)


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
        thread_shared: SharedStateData | None = None
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
            shared_raw = meta.get("shared") or {}
            if isinstance(shared_raw, dict) and shared_raw:
                thread_shared = SharedStateData(
                    memory_files=dict(shared_raw.get("memory_files", {})),
                    notes_md=str(shared_raw.get("notes_md", "")),
                )
            else:
                maybe_memory_files = meta.get("memory_files")
                if isinstance(maybe_memory_files, dict):
                    thread_shared = SharedStateData(memory_files=dict(maybe_memory_files))
        else:
            system_id = system or "cli"
        is_cli_mode = system_id == "cli"
        summary_trigger_chars = parse_summary_trigger_chars(config_dict)

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
            "shared": thread_shared,
        }
        if config_dict.get("use_local_subagents", False):
            state_data["specific_role"] = ORCHESTRATOR_SPECIFIC_ROLE
        state = State(**state_data)
        state.backend_api_mode = backend_api_mode
        state.dialog_steps_offset = 0
        if thread_session is not None:
            meta = getattr(thread_session, "meta", None) or {}
            state.init_user_messages_log(meta.get("user_messages_log"))
            state.init_current_user_messages(meta.get("current_user_messages"))
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
                specific_role=state.specific_role,
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
                if history_exceeds_summary_trigger(
                    state=state,
                    summary_trigger_chars=summary_trigger_chars,
                ):
                    run_summary_compaction(
                        ctx=self._ctx,
                        state=state,
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
        # Seed user_messages_log с текущим пользовательским сообщением.
        # _persist_*_batch-коллбэки ниже получают только step_messages
        # (ответ ассистента + tool calls/outputs); без этого seed'а
        # finish_gate в processing_responses.py читает пустой лог и
        # отвергает валидный submit_solution как «user intent is empty».
        state.append_user_messages_to_log([new_user_message])
        technical_user_message = None
        _thread_mf = thread_shared.memory_files if thread_shared is not None else {}
        _has_memory = (
            isinstance(_thread_mf, dict)
            and any(v.strip() for v in _thread_mf.values() if isinstance(v, str))
        )
        if (
            thread_session is not None
            and _has_memory
            and not history_has_dialog_summary_message(thread_session.history_messages)
        ):
            technical_user_message = {
                "role": "user",
                "content": wrap_technical_user_content(
                    (
                        'client has non-empty memory files; call `memory_files` '
                        'with action="list" to see available files, then read what you need.'
                    ),
                    technical_type=TECHNICAL_TYPE_CURRENT_CONTEXT,
                ),
                "id": generate_message_id_func("m"),
            }
            if logger is not None:
                logger.info(
                    "summary_context_message_added thread_id=%s reason=memory_files_present_no_dialog_summary",
                    thread_session.thread_id,
                )
        elif (
            thread_session is not None
            and _has_memory
            and history_has_dialog_summary_message(thread_session.history_messages)
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
                _shared = getattr(state, "shared", None)
                if isinstance(_shared, SharedStateData):
                    thread_session.meta["shared"] = {
                        "memory_files": _shared.memory_files,
                        "notes_md": _shared.notes_md,
                    }
                state.append_user_messages_to_log(step_messages)
                thread_session.meta["user_messages_log"] = state.user_messages_log
                thread_session.meta["current_user_messages"] = state.current_user_messages
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
                if history_exceeds_summary_trigger(
                    state=state,
                    summary_trigger_chars=summary_trigger_chars,
                ):
                    run_summary_compaction(
                        ctx=self._ctx,
                        state=state,
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

        try:
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
        finally:
            # Закрываем долгоживущие stdio MCP-сессии (напр. browser-use + Chromium),
            # чтобы они не переживали конкретный запуск агента.
            PersistentStdioRegistry.instance().close_all()
            # И persistent-сессии stream_* транспортов (stream_stdio/stream_sse/stream_http):
            # закрываем все открытые ClientSession в их фоновом event loop и гасим loop.
            try:
                _STREAM_SESSION_MANAGER.close_all()
            except Exception:
                pass

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
