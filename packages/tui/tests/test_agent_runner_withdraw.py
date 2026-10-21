"""`AgentRunner.withdraw_message`: забрать реплику из очереди (спека P3 §2.4).

Пока исполнитель строится, реплика лежит в буфере раннера — снимается там.
Исполнитель жив — решает его очередь. Ушедшее отдельным ходом (прогон уже
закрылся) снять нельзя.
"""

from __future__ import annotations

import threading

from gc_core.execution.message_keys import companion_message_id
from gc_testkit.tui_fixtures.stub_executor import StubExecutor
from gc_tui.services.agent_runner import AgentRunner


def _msg(msg_id: str) -> dict:
    return {"role": "user", "content": msg_id, "id": msg_id}


class _BlockingExecutor(StubExecutor):
    """Ход ждёт события: прогон идёт, пока тест его не отпустит."""

    def __init__(self, tmp_path) -> None:
        super().__init__(threads_dir=str(tmp_path))
        self.release = threading.Event()
        self.runs: list[list] = []

    async def arun(self, messages, **kwargs) -> None:
        self.runs.append(list(messages))
        self.release.wait(timeout=5.0)


def test_withdraw_from_prebuild_buffer_without_executor(tmp_path):
    built = StubExecutor(threads_dir=str(tmp_path))
    entered = threading.Event()
    release = threading.Event()

    def factory():
        entered.set()
        release.wait(timeout=5.0)
        return built

    runner = AgentRunner(executor_factory=factory)
    try:
        assert runner.start(query="x", thread_id="t1") is True
        assert entered.wait(timeout=2.0)
        runner.stop()  # прогон после сборки не нужен — только перелив
        runner.add_message(_msg("a"))
        runner.add_message(_msg(companion_message_id("a")))
        runner.add_message(_msg("b"))

        assert runner.withdraw_message("a") is True
        assert runner.withdraw_message("zzz") is False
    finally:
        release.set()
        runner.wait(timeout=2.0)
    assert [m["id"] for m in built.queued] == ["b"]


def test_withdraw_delegates_to_live_executor(tmp_path):
    ex = StubExecutor(threads_dir=str(tmp_path))
    runner = AgentRunner(executor=ex)
    runner.add_message(_msg("a"))
    runner.add_message(_msg(companion_message_id("a")))

    assert runner.withdraw_message("a") is True
    assert ex.queued == []
    assert runner.withdraw_message("a") is False


def test_withdraw_refused_after_run_closed_and_tail_spawned(tmp_path):
    ex = _BlockingExecutor(tmp_path)
    runner = AgentRunner(executor=ex)
    try:
        ex.release.set()
        assert runner.start(query="первый", thread_id="t1") is True
        runner.wait(timeout=2.0)
        assert not runner.is_running()

        ex.release.clear()
        runner.add_message(_msg("late"))  # закрытый прогон → свой ход

        assert runner.withdraw_message("late") is False
    finally:
        ex.release.set()
        runner.wait(timeout=2.0)
    assert ex.runs[-1] == ["late"]
