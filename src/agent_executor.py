"""
Потокобезопасный исполнитель агента.

Один мьютекс на экземпляр, держится кратко (доступ к state / pending / stop).
State создаётся заново при каждом run(). Возвращённый из get_state() state
нельзя мутировать снаружи; изменения — только через add_message() или под get_lock().
"""

import os
import threading
from typing import Any

from processing_responses import run_agent
from run_context import RunContext
from states import State, save_state, dump_storage
from thread_summarizer import (
    collect_user_messages_from_history,
    summarize_dialog_via_chat_completions,
)
from thread_store import (
    ThreadSession,
    calc_messages_chars_total,
    reset_thread_files,
    resolve_threads_dir,
)
from tools.skills.registry import get_skills_registry
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

        def _without_system_messages(messages_list: list[dict]) -> list[dict]:
            out: list[dict] = []
            for msg in messages_list:
                if not isinstance(msg, dict):
                    continue
                if str(msg.get("role", "")).strip().lower() == "system":
                    continue
                out.append(msg)
            return out

        def _contains_dialog_summary_message(messages_list: list[dict]) -> bool:
            for msg in messages_list:
                if not isinstance(msg, dict):
                    continue
                if str(msg.get("role", "")).strip().lower() != "user":
                    continue
                content = msg.get("content")
                if not isinstance(content, str):
                    continue
                if "Summarized context from previous dialogue." in content:
                    return True
                if "<dialog_summary>" in content:
                    return True
            return False

        def _run_summary_compaction_if_needed(
            *,
            trigger_reason: str,
            messages_for_summary: list[dict],
            chars_total: int,
        ) -> None:
            if logger is not None:
                logger.info(
                    "summary_triggered reason=%s thread_id=%s system=%s chars_total=%s threshold=%s",
                    trigger_reason,
                    thread_session.thread_id if thread_session is not None else None,
                    thread_session.system_id if thread_session is not None else None,
                    chars_total,
                    summary_trigger_chars,
                )
            if is_cli_mode:
                print(
                    "[summary] Запущена суммаризация: "
                    f"thread={thread_session.thread_id}, chars={chars_total}, "
                    f"threshold={summary_trigger_chars}"
                )
            summary_result = summarize_dialog_via_chat_completions(
                ctx=self._ctx,
                state=state,
                full_history_messages=messages_for_summary,
                current_memory=(state.notes_md or "") if isinstance(state.notes_md, str) else "",
            )
            summary_content = summary_result["summary_content"]
            raw_summary = str(summary_result.get("raw_summary") or "")
            memory_additions = summary_result.get("memory_additions", "")
            if logger is not None:
                logger.info("summary_received_raw_text=\n%s", raw_summary)
                logger.info("summary_received_content_text=\n%s", summary_content)
            if is_cli_mode:
                print("[summary] Полученный raw summary:")
                print(raw_summary)
                print("[summary] Полученный summary content:")
                print(summary_content)
            notes_base = (state.notes_md or "") if isinstance(state.notes_md, str) else ""
            notes_new = notes_base
            if memory_additions:
                notes_new = (notes_base + "\n\n" + memory_additions).strip() if notes_base else memory_additions.strip()
            if not notes_new:
                notes_new = "## Thread summary\n\n" + summary_result["raw_summary"][:10_000]
            compact_res = thread_session.archive_and_compact_with_summary(
                summary_text=summary_content,
                notes_md=notes_new,
                user_messages_log=summary_result.get("user_messages_log"),
            )
            new_chars = calc_messages_chars_total(
                _without_system_messages(thread_session.get_full_history_messages())
            )
            ratio = round((new_chars / chars_total), 4) if chars_total > 0 else 0.0
            if logger is not None:
                logger.info(
                    "summary_completed reason=%s thread_id=%s system=%s old_chars=%s new_chars=%s compression_ratio=%s archive_suffix=%s num_batches=%s memory_additions_chars=%s",
                    trigger_reason,
                    thread_session.thread_id,
                    thread_session.system_id,
                    chars_total,
                    new_chars,
                    ratio,
                    compact_res.get("archive_suffix"),
                    summary_result.get("num_batches"),
                    len(memory_additions or ""),
                )
            if is_cli_mode:
                print(
                    "[summary] Завершена: "
                    f"old_chars={chars_total}, new_chars={new_chars}, ratio={ratio}, "
                    f"batches={summary_result.get('num_batches')}, "
                    f"memory_additions_chars={len(memory_additions or '')}"
                )
            state.notes_md = notes_new
            state.previous_response_id = None

        if (
            thread_session is not None
            and backend_api_mode == "chat_completions"
            and summary_trigger_chars > 0
        ):
            history_total = len(thread_session.history_messages)
            history_wo_system = _without_system_messages(thread_session.history_messages)
            if logger is not None:
                logger.info(
                    "summary_precheck_pre_run thread_id=%s history_total=%s history_wo_system=%s removed_system=%s threshold=%s",
                    thread_session.thread_id,
                    history_total,
                    len(history_wo_system),
                    history_total - len(history_wo_system),
                    summary_trigger_chars,
                )
            chars_before_run = calc_messages_chars_total(history_wo_system)
            if chars_before_run >= summary_trigger_chars:
                try:
                    _run_summary_compaction_if_needed(
                        trigger_reason="pre_run",
                        messages_for_summary=history_wo_system,
                        chars_total=chars_before_run,
                    )
                except Exception as summarize_exc:
                    if logger is not None:
                        logger.warning(
                            "summary_failed reason=pre_run thread_id=%s error=%s",
                            thread_session.thread_id,
                            summarize_exc,
                            exc_info=True,
                        )
                    if is_cli_mode:
                        print(
                            "[summary] Ошибка суммаризации: "
                            f"thread={thread_session.thread_id}, error={summarize_exc}"
                        )
            elif logger is not None:
                logger.info(
                    "summary_skip reason=pre_run_below_threshold thread_id=%s chars_total=%s threshold=%s",
                    thread_session.thread_id,
                    chars_before_run,
                    summary_trigger_chars,
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
                "content": (
                    '<current_context>'
                    'client has non-empty thread notes; call `thread_notes` '
                    'with action="read" to load them.'
                    '</current_context>'
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

            def _persist_thread_batch(batch_messages: list[dict]) -> None:
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
                existing_user_log_raw = thread_session.meta.get("user_messages_log")
                existing_user_log = (
                    [str(x) for x in existing_user_log_raw if isinstance(x, str)]
                    if isinstance(existing_user_log_raw, list)
                    else []
                )
                for msg in batch_messages:
                    if not isinstance(msg, dict):
                        continue
                    items = collect_user_messages_from_history([msg])
                    if not items:
                        continue
                    text = items[0]
                    if text not in existing_user_log:
                        existing_user_log.append(text)
                thread_session.meta["user_messages_log"] = existing_user_log
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
                    batch_messages, previous_response_id=state.previous_response_id
                )
                if (
                    state.backend_api_mode != "chat_completions"
                    or summary_trigger_chars <= 0
                ):
                    return
                full_messages = _without_system_messages(
                    thread_session.get_full_history_messages()
                )
                if logger is not None:
                    history_now_total = len(thread_session.get_full_history_messages())
                    logger.info(
                        "summary_precheck_during_run thread_id=%s history_total=%s history_wo_system=%s removed_system=%s threshold=%s",
                        thread_session.thread_id,
                        history_now_total,
                        len(full_messages),
                        history_now_total - len(full_messages),
                        summary_trigger_chars,
                    )
                chars_total = calc_messages_chars_total(full_messages)
                if chars_total < summary_trigger_chars:
                    if logger is not None:
                        logger.info(
                            "summary_skip reason=during_run_below_threshold thread_id=%s chars_total=%s threshold=%s",
                            thread_session.thread_id,
                            chars_total,
                            summary_trigger_chars,
                        )
                    return
                try:
                    _run_summary_compaction_if_needed(
                        trigger_reason="during_run",
                        messages_for_summary=full_messages,
                        chars_total=chars_total,
                    )
                except Exception as summarize_exc:
                    if logger is not None:
                        logger.warning(
                            "summary_failed reason=during_run thread_id=%s error=%s",
                            thread_session.thread_id,
                            summarize_exc,
                            exc_info=True,
                        )
                    if is_cli_mode:
                        print(
                            "[summary] Ошибка суммаризации: "
                            f"thread={thread_session.thread_id}, error={summarize_exc}"
                        )

            persist_messages_callback = _persist_thread_batch
            get_full_history_messages = thread_session.get_full_history_messages

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
