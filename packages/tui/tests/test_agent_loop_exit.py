"""Выход из приложения при ходе, глухом к стопу (B′1, Task 4).

Закрытие цикла агента отменяет то, что ещё на нём идёт, — даже если
исполнитель стоп не слышит.
"""

from __future__ import annotations

import asyncio
import threading

from gc_tui.services.agent_runner import AgentRunner


class _DeafExecutor:
    """Исполнитель: начал ход и ждёт вечно; `stop_run` ничего не делает."""

    def __init__(self) -> None:
        self.started = threading.Event()

    async def arun(self, messages, *, thread_id, **kw):
        self.started.set()
        await asyncio.Event().wait()

    def stop_run(self) -> None:
        pass

    def add_message(self, message) -> None:
        pass


def test_deaf_turn_is_cancelled_by_shutdown(agent_loop, monkeypatch):
    thread_errors: list = []
    monkeypatch.setattr(
        threading,
        "excepthook",
        lambda args: thread_errors.append(args.exc_type),
    )
    executor = _DeafExecutor()
    runner = AgentRunner(executor=executor, loop=agent_loop)
    assert runner.start(query="q", thread_id="t1")
    assert executor.started.wait(5.0)
    runner.stop()
    agent_loop.shutdown(timeout=2.0)
    runner.wait(5.0)
    assert not agent_loop.thread.is_alive()
    assert not runner.is_running()
    assert thread_errors == []
