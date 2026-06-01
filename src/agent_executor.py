"""
Потокобезопасный исполнитель агента.

Один мьютекс на экземпляр, держится кратко (доступ к state / pending / stop).
State создаётся заново при каждом run(). Возвращённый из get_state() state
нельзя мутировать снаружи; изменения — только через add_message() или под get_lock().
"""

import os
import threading
from copy import copy as _shallow_copy_msg
from typing import Any

from processing_responses import run_agent
from run_context import RunContext
from config_modules.stored_outputs import get_stored_tool_outputs_max_entries
from message_policy.chat_completions import repair_tool_call_pairing
from run_summary_coordinator import RunSummaryCoordinator
from summarize.config import parse_summary_trigger_chars
from persistence.state_storage import dump_storage, save_state
from persistence.thread_meta_mapper import ThreadMetaMapper
from intent_log.user_turn import append_user_ask_deduped, append_user_asks_from_messages
from message_policy.user_message_policy import extract_real_user_message_text
from states import State, SharedStateData
from thread_store import (
    ThreadSession,
    make_message_key,
    reset_thread_files,
    resolve_threads_dir,
)
from tools.connect_mcp.persistent_stdio import PersistentStdioRegistry
from tools.connect_mcp.stream_client import _SESSION_MANAGER as _STREAM_SESSION_MANAGER
from tools.skills.registry import get_skills_registry
from message_policy.tool_usage_notice.state import persist_tool_usage_notice_state
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func
from utils.local_render_system_prompt import render_system_prompt

ORCHESTRATOR_SPECIFIC_ROLE = (
    "You are an orchestrator agent. You may execute work yourself or delegate to subagents when that clearly pays off.\n"
    "- Handle simple, local tasks yourself (small edits, one-off questions, quick checks) — do not delegate by default.\n"
    "- Delegate only when the task is genuinely complex, parallelizable, or needs a specialized role "
    "(e.g. deep review, large exploration) and delegation tools are listed in `<tools>`.\n"
    "- For complex work, decompose into subtasks and delegate appropriately; validate outcomes yourself "
    "or via a review subagent when quality or risk warrants it.\n"
    "- At the start of a large task, use a planner subagent only when planning overhead is justified.\n"
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
        messages: list[str],
        *,
        thread_id: str | None = None,
        start_new_thread: bool = False,
        system: str | None = None,
        verbose: bool | None = None,
    ) -> Any:
        """Запускает агента. При каждом вызове state создаётся заново.

        messages: непустой список текстов — каждый элемент уходит отдельным user-сообщением.
        """
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
            if backend_api_mode == "chat_completions":
                thread_session.history_messages = repair_tool_call_pairing(
                    thread_session.history_messages,
                    keep_latest_unanswered=False,
                )
            if backend_api_mode == "responses":
                previous_response_id = thread_session.previous_response_id
            meta = getattr(thread_session, "meta", None) or {}
            thread_shared = ThreadMetaMapper.shared_from_meta(meta)
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
            "workspace_config": config_dict,
        }
        if config_dict.get("use_local_subagents", False):
            state_data["specific_role"] = ORCHESTRATOR_SPECIFIC_ROLE
        state = State(**state_data)
        state.tool_call_outputs_store.set_max_entries(
            get_stored_tool_outputs_max_entries(config_dict)
        )
        state.backend_api_mode = backend_api_mode
        state.dialog_steps_offset = 0
        if thread_session is not None:
            meta = getattr(thread_session, "meta", None) or {}
            state.init_user_intent_log(meta.get("user_intent_log"))
            state.init_current_user_intent(meta.get("current_user_intent"))
            state.init_user_turn_seq(meta.get("user_turn_seq"))
            state.infer_user_turn_seq_if_missing()
            state.init_tool_usage_notice_from_meta(meta)
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

        if (
            thread_session is not None
            and backend_api_mode == "chat_completions"
            and thread_session.history_messages
        ):
            state.messages.extend(
                [
                    _shallow_copy_msg(m)
                    for m in thread_session.history_messages
                    if isinstance(m, dict)
                ]
            )

        summary = RunSummaryCoordinator(
            ctx=self._ctx,
            state=state,
            thread_session=thread_session,
            summary_trigger_chars=summary_trigger_chars,
            is_cli_mode=is_cli_mode,
            pre_backend_trigger_reason="pre_backend",
            logger=logger,
        )

        summary.pre_run_compact_if_needed(trigger_reason="pre_run")

        if not messages:
            raise ValueError(
                "Список пользовательских сообщений не должен быть пустым."
            )
        user_bodies = [str(x) for x in messages]
        new_user_messages = [
            {
                "role": "user",
                "content": body,
                "id": generate_message_id_func("m"),
            }
            for body in user_bodies
        ]
        # На старте user turn дописываем текущий CLI-запрос в current_user_intent
        # (только реальные user-сообщения, не technical). user_intent_log — только
        # user_intent_log — только после flush при accept submit_solution.
        for new_user_message in new_user_messages:
            state.finish_gate_rejection_count = 0
            text = extract_real_user_message_text(new_user_message)
            if text:
                state.current_user_intent = append_user_ask_deduped(
                    state.current_user_intent,
                    text,
                    user_turn_seq=state.user_turn_seq,
                )

        messages = list(new_user_messages)

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
            messages = [*base_messages, *new_user_messages]

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
                    thread_session.meta = ThreadMetaMapper.merge_shared_into_meta(
                        thread_session.meta, _shared
                    )
                known_before = set(thread_session.known_message_keys)
                step_messages_for_user_scope = [
                    m for m in step_messages
                    if make_message_key(m) not in known_before
                ]
                state.current_user_intent = append_user_asks_from_messages(
                    state.current_user_intent,
                    step_messages_for_user_scope,
                    user_turn_seq=state.user_turn_seq,
                )
                thread_session.meta = ThreadMetaMapper.merge_state_into_meta(
                    thread_session.meta, state
                )
                thread_session.meta["backend_api_mode"] = state.backend_api_mode
                thread_session.backend_api_mode = state.backend_api_mode
                persist_tool_usage_notice_state(state, thread_session.meta)
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
                state.current_user_intent = append_user_asks_from_messages(
                    state.current_user_intent,
                    step_messages,
                    user_turn_seq=state.user_turn_seq,
                )

            persist_messages_callback = _persist_runtime_batch

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
                pre_backend_compaction_hook=summary.pre_backend_compaction_hook,
                get_pending_messages=get_pending_messages,
                check_stop=check_stop,
                session_meta=(
                    thread_session.meta
                    if thread_session is not None
                    else None
                ),
            )
            if thread_session is not None:
                thread_session.meta = ThreadMetaMapper.merge_state_into_meta(
                    thread_session.meta, state
                )
                thread_session.meta["backend_api_mode"] = state.backend_api_mode
                persist_tool_usage_notice_state(state, thread_session.meta)
                thread_session.persist_batch(
                    [], previous_response_id=state.previous_response_id
                )
        finally:
            # Закрываем долгоживущие stdio MCP-сессии (напр. @playwright/mcp + Chromium),
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
