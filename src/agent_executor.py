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
        verbose: bool | None = None,
    ) -> Any:
        """Запускает агента. При каждом вызове state создаётся заново."""
        config_dict = self._ctx.config_dict
        effective_verbose = (
            verbose if verbose is not None else config_dict.get("verbose", False)
        )
        skills_list = get_skills_registry(config_dict).list_skills()
        state_data = {
            "parent_state_id": None,
            "verbose": effective_verbose,
            "model": config_dict["model"],
            "tools_spec": self._ctx.tools_spec,
            "tools_descr": self._ctx.tools_description,
            "max_steps": config_dict["max_steps"],
            "skills_list": skills_list,
            "system_prompt_id": config_dict["system_prompt_id"],
            "use_server_subagents": config_dict.get("use_server_subagents", False),
            "agent_md_path": config_dict.get("agent_md_path"),
        }
        state = State(**state_data)
        save_state(state)

        with self._lock:
            self._state = state
            self._pending_messages = []

        new_user_message = {
            "role": "user",
            "content": message,
            "id": generate_message_id_func("m"),
        }
        messages = [new_user_message]

        thread_session: ThreadSession | None = None
        persist_messages_callback = None
        get_full_history_messages = None
        if thread_id is not None:
            thread_session = ThreadSession.from_config(
                config_dict=config_dict, thread_id=thread_id
            )
            state.previous_response_id = thread_session.previous_response_id
            messages = thread_session.build_initial_messages(new_user_message)
            thread_session.set_runtime_messages(state.messages)

            def _persist_thread_batch(batch_messages: list[dict]) -> None:
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
            cur_dir = os.getcwd()
            dump_storage(cur_dir, indent=2)

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
