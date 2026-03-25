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
from thread_store import ThreadSession
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
        system: str | None = None,
        verbose: bool | None = None,
    ) -> Any:
        """Запускает агента. При каждом вызове state создаётся заново."""
        config_dict = self._ctx.config_dict
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
