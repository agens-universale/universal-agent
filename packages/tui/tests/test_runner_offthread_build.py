"""Сборка executor (включая MCP-дискавери с сетевыми вызовами) не должна
блокировать UI-поток. AgentRunner строит executor лениво в своём worker-потоке;
get_runner() на UI-потоке больше не вызывает _build_real_executor синхронно.

Регрессия: при включённых mcp-servers Enter в сессии замораживал event-loop
Textual, пока _build_tools_registry опрашивал серверы (retry+timeout)."""
import threading
import time

import pytest

from gc_tui.services.agent_runner import AgentRunner


class _FakeExecutor:
    def __init__(self):
        self.messages_added: list[dict] = []
        self._event = threading.Event()

    async def arun(self, messages, *, thread_id, start_new_thread=False, system=None,
            verbose=None, discard_writes_after_stop=False):
        self._event.wait(timeout=2.0)

    def stop_run(self):
        self._event.set()

    def add_message(self, m):
        self.messages_added.append(m)


def test_factory_not_invoked_until_start():
    calls = []
    runner = AgentRunner(executor_factory=lambda: (calls.append(1) or _FakeExecutor()))
    assert calls == []                       # сборка отложена, не на конструкции
    runner.start(query="x", thread_id="t1")
    time.sleep(0.1)
    assert calls == [1]                       # построен при старте
    runner.stop(); runner.wait(1.0)


def test_factory_runs_off_caller_thread():
    built = {}

    def factory():
        built["thread"] = threading.current_thread().name
        return _FakeExecutor()

    runner = AgentRunner(executor_factory=factory)
    runner.start(query="x", thread_id="t1")
    time.sleep(0.1)
    assert "thread" in built
    assert built["thread"] != threading.current_thread().name  # построен в worker-потоке
    runner.stop(); runner.wait(1.0)


def test_messages_before_build_are_buffered():
    fake = _FakeExecutor()
    runner = AgentRunner(executor_factory=lambda: fake)
    runner.add_message({"role": "user", "content": "early"})
    assert fake.messages_added == []          # executor ещё не построен — буфер
    runner.start(query="x", thread_id="t1")
    time.sleep(0.1)
    assert fake.messages_added == [{"role": "user", "content": "early"}]  # долит после сборки
    runner.stop(); runner.wait(1.0)


@pytest.mark.asyncio
async def test_warm_builds_executor_off_ui_thread(tmp_path):
    # on_mount запускает warm() — сборка executor (incl. MCP-дискавери) идёт в
    # worker-потоке, НЕ на UI/main (иначе фриз event-loop Textual).
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    built = []
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: (
        built.append(threading.current_thread().name) or _FakeExecutor()
    )
    main_name = threading.main_thread().name
    async with app.run_test() as pilot:
        await pilot.pause(0.5)                 # даём warm-потоку собрать
        assert built, "warm должен собрать executor на старте"
        assert built[0] != main_name           # сборка не на UI/main-потоке
