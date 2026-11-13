"""Один долгоживущий цикл агента на приложение (B′1, Task 2).

Раньше каждый ход шёл своим ``asyncio.run`` в свежем потоке: задачи,
заведённые ходом, умирали с концом хода, а пул httpx закрывался после
каждого. Фоновым субагентам нужен цикл, который переживает ход.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
import time
from types import SimpleNamespace

import pytest
from gc_testkit import as_coro
from gc_testkit.tui_support import tui_settings, wait_until
from gc_tui.services.agent_loop import AgentLoop
from gc_tui.services.agent_runner import AgentRunner


class _LoopRecorder:
    """Исполнитель: запоминает цикл хода, умеет завести задачу дольше хода."""

    def __init__(self) -> None:
        self.loops: list[asyncio.AbstractEventLoop] = []
        self.spawn_on: asyncio.Event | None = None
        self.spawned: asyncio.Task | None = None
        self.hold: asyncio.Event | None = None
        self.entered = threading.Event()

    async def arun(self, messages, *, thread_id, **kw):
        self.loops.append(asyncio.get_running_loop())
        if self.spawn_on is not None:
            self.spawned = asyncio.ensure_future(self._wait(self.spawn_on))
        self.entered.set()
        if self.hold is not None:
            await self.hold.wait()

    @staticmethod
    async def _wait(event: asyncio.Event) -> str:
        await event.wait()
        return "done"

    def stop_run(self) -> None:
        pass

    def add_message(self, message) -> None:
        pass


class _Gate:
    """Ворота на цикле агента: ``set`` с чужого потока, без гонки."""

    def __init__(self, loop: AgentLoop, event: asyncio.Event) -> None:
        self._loop = loop
        self._event = event

    def set(self) -> None:
        self._loop.call_soon(self._event.set)


class _FakeExecutorApp:
    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop
        self.executor = _LoopRecorder()
        self.runner = AgentRunner(executor=self.executor, loop=loop)

    def _turn(self) -> None:
        assert self.runner.start(query="q", thread_id="t1")
        self.runner.wait(5.0)
        assert not self.runner.is_running()

    def loops_of_two_turns(self):
        self._turn()
        self._turn()
        first, second = self.executor.loops
        return first, second

    def turn_that_spawns_task_waiting_on_event(self) -> _Gate:
        event = self.loop.run(_make_event())
        self.executor.spawn_on = event
        assert self.runner.start(query="q", thread_id="t1")
        assert self.executor.entered.wait(5.0)
        return _Gate(self.loop, event)

    def end_turn(self) -> None:
        self.runner.wait(5.0)
        assert not self.runner.is_running()

    def spawned_task_result(self):
        task = self.executor.spawned
        return self.loop.run(_await(task), timeout=5.0)


async def _make_event() -> asyncio.Event:
    return asyncio.Event()


async def _await(task):
    return await task


@pytest.fixture
def fake_executor_app(agent_loop):
    return _FakeExecutorApp(agent_loop)


def test_two_turns_same_loop(fake_executor_app):
    first, second = fake_executor_app.loops_of_two_turns()
    assert first is second


def test_task_started_in_turn_survives_turn_end(fake_executor_app):
    gate = fake_executor_app.turn_that_spawns_task_waiting_on_event()
    fake_executor_app.end_turn()
    gate.set()
    assert fake_executor_app.spawned_task_result() == "done"


class _CompactingExecutor:
    async def acompact_thread(self, thread_id, system=None):
        return True

    def stop_run(self) -> None:
        pass


class _AppOnLoop:
    """Настоящее окно (не смонтировано) с подменённой сборкой контекста."""

    def __init__(self, tmp_path, monkeypatch) -> None:
        from gc_tui.app import TuiApp

        self.app = TuiApp(
            threads_dir=str(tmp_path),
            system="cli",
            settings=tui_settings({"agent.model": "M"}),
        )
        self.loop = self.app.agent_loop
        monkeypatch.setattr(
            "gc_core.config_modules.auth.build_auth_connector",
            lambda cfg: None,
        )
        monkeypatch.setattr(
            "gc_core.run_context.acreate_run_context",
            as_coro(lambda *a, **k: _ctx_double()),
        )
        monkeypatch.setattr(
            "gc_core.agent_executor.AgentExecutor", lambda ctx: ctx
        )

    def open_client_on_loop(self):
        from gc_core.utils.async_http import get_async_client

        return self.loop.run(as_coro(get_async_client)())

    def rebuild_ctx(self) -> None:
        self.app._build_real_executor()

    def run_compact(self) -> None:
        runner = AgentRunner(executor=_CompactingExecutor(), loop=self.loop)
        assert runner.compact_now("t1", "cli")
        runner.wait(5.0)
        assert not runner.is_running()


def _ctx_double():
    return SimpleNamespace(
        os_backend=None,
        tools_data={},
        mcp_status={},
        logging_path="/tmp/agent_main_test.log",
        registered_tool_names=None,
    )


@pytest.fixture
def app_on_loop(tmp_path, monkeypatch):
    return _AppOnLoop(tmp_path, monkeypatch)


def test_ctx_build_and_compact_keep_live_clients(app_on_loop):
    client = app_on_loop.open_client_on_loop()
    app_on_loop.rebuild_ctx()
    app_on_loop.run_compact()
    assert not client.is_closed


def test_ctx_build_runs_on_the_agent_loop(app_on_loop, monkeypatch):
    """Сборка контекста — на цикле агента: MCP-сессии и клиенты, открытые
    сборкой, живут на том же цикле, что и ход."""
    seen = []

    async def _fake(*_args, **_kwargs):
        seen.append(threading.current_thread().name)
        return _ctx_double()

    monkeypatch.setattr("gc_core.run_context.acreate_run_context", _fake)
    app_on_loop.rebuild_ctx()
    assert seen == ["gc-ua-agent-loop"]


class _Spy:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


@pytest.fixture
def spy_aclose_clients(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr("gc_core.utils.async_http.aclose_clients", spy)
    return spy


def test_shutdown_closes_clients_and_joins(agent_loop, spy_aclose_clients):
    agent_loop.start()
    agent_loop.shutdown()
    assert spy_aclose_clients.calls == 1 and not agent_loop.thread.is_alive()


def test_concurrent_shutdown_closes_once(agent_loop, monkeypatch, caplog):
    """Два `shutdown` наперегонки (воркер раннера и teardown): цикл
    останавливает первый, второй только ждёт поток — его ``loop.stop``
    оборвал бы уборку, и закрытие клиентов упало бы в лог.

    Порядок — событиями: уборка первого держится в ``aclose_clients``, пока
    второй не дошёл до своего шага — до ``join`` потока."""
    caplog.set_level(logging.WARNING, logger="gc_tui.services.agent_loop")
    entered, release = threading.Event(), threading.Event()
    second_arrived = threading.Event()
    calls: list[int] = []

    async def _aclose() -> None:
        calls.append(1)
        if len(calls) > 1:
            second_arrived.set()
        entered.set()
        await asyncio.to_thread(release.wait, 5)

    monkeypatch.setattr("gc_core.utils.async_http.aclose_clients", _aclose)
    agent_loop.start()
    real_join = agent_loop.thread.join

    def _join(timeout=None):
        if threading.current_thread().name == "second-shutdown":
            second_arrived.set()
        real_join(timeout)

    agent_loop.thread.join = _join
    errors: list[BaseException] = []

    def _shutdown() -> None:
        try:
            agent_loop.shutdown()
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=_shutdown)
    first.start()
    assert entered.wait(5)  # уборка первого закрывает клиенты на цикле
    second = threading.Thread(target=_shutdown, name="second-shutdown")
    second.start()
    assert second_arrived.wait(5)  # второй внутри shutdown, цикл ещё жив
    release.set()
    first.join(5)
    second.join(5)
    assert errors == []
    assert calls == [1]
    assert not agent_loop.thread.is_alive()
    assert "agent_loop_aclose_clients_failed" not in caplog.text


def test_shutdown_closes_clients_opened_by_cancelled_tasks(agent_loop):
    """Выход сначала отменяет задачи, потом закрывает пул: клиент, взятый
    уборкой отменённой задачи, тоже закрыт."""
    from gc_core.utils.async_http import get_async_client

    started = threading.Event()
    opened: list = []

    async def _deaf_turn() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            opened.append(get_async_client())

    agent_loop.submit(_deaf_turn())
    assert started.wait(5)
    agent_loop.shutdown()
    assert len(opened) == 1
    assert opened[0].is_closed


def test_start_is_idempotent_and_names_the_thread(agent_loop):
    agent_loop.start()
    thread = agent_loop.thread
    agent_loop.start()
    assert agent_loop.thread is thread
    assert thread.name == "gc-ua-agent-loop" and thread.daemon


async def _inside_run(loop: AgentLoop):
    return loop.run(asyncio.sleep(0))


def test_run_from_loop_thread_raises(agent_loop):
    agent_loop.start()
    with pytest.raises(RuntimeError):
        agent_loop.run(_inside_run(agent_loop))


@pytest.fixture
async def app_pilot_with_loop(tmp_path):
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = _LoopRecorder
    async with app.run_test() as pilot:
        yield SimpleNamespace(app=app, pilot=pilot, agent_loop=app.agent_loop)


async def test_run_from_ui_thread_raises(app_pilot_with_loop):
    with pytest.raises(RuntimeError):
        app_pilot_with_loop.agent_loop.run(asyncio.sleep(0))


async def test_ui_awaits_the_loop_through_a_future(app_pilot_with_loop):
    """Разрешённый путь с UI: ``await wrap_future(loop.submit(...))``."""
    loop = app_pilot_with_loop.agent_loop
    name = await asyncio.wrap_future(loop.submit(_thread_name()))
    assert name == "gc-ua-agent-loop"


async def _thread_name() -> str:
    return threading.current_thread().name


async def test_unmount_shuts_the_loop_down(tmp_path):
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = _LoopRecorder
    async with app.run_test():
        loop = app.agent_loop
        loop.start()
    assert not loop.thread.is_alive()


class _RunnerOnLoop:
    def __init__(self, loop: AgentLoop, monkeypatch) -> None:
        self.loop = loop
        self.executor = _LoopRecorder()
        self.errors: list[BaseException] = []
        self.runner = AgentRunner(
            executor=self.executor, loop=loop, on_error=self.errors.append
        )
        self.endings: list[str] = []
        real = AgentRunner._close_or_take_tail

        def _spy(runner, executor, *, ending="", lost=()):
            self.endings.append(ending)
            return real(runner, executor, ending=ending, lost=lost)

        monkeypatch.setattr(AgentRunner, "_close_or_take_tail", _spy)
        # Отмена не должна ронять поток воркера с трейсбэком.
        self.thread_errors: list[type] = []
        monkeypatch.setattr(
            threading,
            "excepthook",
            lambda args: self.thread_errors.append(args.exc_type),
        )

    def cancel_mid_turn(self):
        self.executor.hold = self.loop.run(_make_event())
        assert self.runner.start(query="q", thread_id="t1")
        assert self.executor.entered.wait(5.0)
        self.loop.run(_cancel_tasks_except_current())
        self.runner.wait(5.0)
        assert not self.runner.is_running()
        assert self.errors == []
        assert self.thread_errors == []
        return SimpleNamespace(outcome=self.endings[-1])

    def run_plain_turn(self) -> None:
        assert self.runner.start(query="q", thread_id="t1")
        self.runner.wait(5.0)
        assert not self.runner.is_running()

    def live_loop_tasks(self) -> list:
        return self.loop.run(_other_tasks())


async def _other_tasks() -> list:
    me = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not me]


async def _cancel_tasks_except_current() -> None:
    for task in await _other_tasks():
        task.cancel()


@pytest.fixture
def runner_on_loop(agent_loop, monkeypatch):
    return _RunnerOnLoop(agent_loop, monkeypatch)


def test_cancelled_future_is_cancelled_outcome(runner_on_loop):
    assert runner_on_loop.cancel_mid_turn().outcome == "cancelled"


def test_no_live_tasks_after_turn_without_subagents(runner_on_loop):
    runner_on_loop.run_plain_turn()
    assert runner_on_loop.live_loop_tasks() == []


def test_runner_without_loop_owns_one_and_releases_it():
    executor = _LoopRecorder()
    runner = AgentRunner(executor=executor)
    assert runner.start(query="q", thread_id="t1")
    runner.wait(5.0)
    thread = runner._loop.thread
    assert thread.is_alive()
    runner.release()
    assert not thread.is_alive()


def test_runner_on_a_shared_loop_leaves_it_running(agent_loop):
    runner = AgentRunner(executor=_LoopRecorder(), loop=agent_loop)
    assert runner.start(query="q", thread_id="t1")
    runner.wait(5.0)
    runner.release()
    assert agent_loop.thread.is_alive()


def test_busy_loop_is_logged(agent_loop, caplog):
    """Сторож: цикл, занятый дольше ``busy_warn_sec``, виден в логе."""
    assert AgentLoop.busy_warn_sec == 10.0
    now = [100.0]
    agent_loop._clock = lambda: now[0]
    caplog.set_level(logging.WARNING, logger="gc_tui.services.agent_loop")
    agent_loop._check_lag(expected=95.0)
    assert caplog.records == []
    now[0] = 111.0
    agent_loop._check_lag(expected=100.0)
    messages = [r.getMessage().split(" (")[0] for r in caplog.records]
    assert messages == ["цикл агента занят > 10 с"]


def test_foreign_input_on_the_loop_reads_the_app_queue(agent_loop):
    """Мост stdin живёт на потоке цикла: чужой ``input()`` в корутине
    читает очередь приложения, а не терминал Textual."""
    agent_loop.stdin_queue.put("ответ\n")
    assert agent_loop.run(as_coro(input)(), timeout=5.0) == "ответ"


def test_next_runner_keeps_the_stop_wakeup(agent_loop):
    """Стоп будит чужой ``input()`` пустой строкой в общей очереди stdin.
    Старт следующего раннера её не забирает: ход остановленного раннера
    может дойти до ``input()`` позже и без неё держал бы весь цикл."""
    stopped = AgentRunner(executor=_LoopRecorder(), loop=agent_loop)
    stopped.stop()
    following = AgentRunner(executor=_LoopRecorder(), loop=agent_loop)
    assert following.start(query="q", thread_id="t1")
    following.wait(5.0)
    assert agent_loop.stdin_queue.get_nowait() == "\n"


def test_submit_after_shutdown_raises_instead_of_hanging(agent_loop):
    """Корутина, отданная закрытому циклу, не ждёт вечно: отказ сразу,
    сама корутина закрыта (иначе «never awaited»)."""
    agent_loop.start()
    agent_loop.shutdown()
    with pytest.raises(RuntimeError):
        agent_loop.submit(asyncio.sleep(0))
    with pytest.raises(RuntimeError):
        agent_loop.run(asyncio.sleep(0), timeout=5.0)


def test_watchdog_is_scheduled_after_start(agent_loop, monkeypatch):
    ticked = threading.Event()
    monkeypatch.setattr(
        agent_loop, "_check_lag", lambda expected: ticked.set()
    )
    agent_loop.start()
    assert ticked.wait(5.0)


async def test_shutdown_from_ui_thread_raises(app_pilot_with_loop):
    loop = app_pilot_with_loop.agent_loop
    loop.start()
    with pytest.raises(RuntimeError):
        loop.shutdown()
    assert loop.thread.is_alive()


async def test_owned_loop_release_from_ui_thread_does_not_block():
    """Раннер без общего цикла (тесты), отпущенный с потока UI: свой цикл
    гасится в стороннем потоке, а не синхронным ожиданием на UI."""
    runner = AgentRunner(executor=_LoopRecorder())
    assert runner.start(query="q", thread_id="t1")
    await asyncio.to_thread(runner.wait, 5.0)
    thread = runner._loop.thread
    runner.release()
    await asyncio.to_thread(thread.join, 5.0)
    assert not thread.is_alive()


def _hold_ui(loop: AgentLoop, call) -> bool:
    """Вызвать ``call`` на цикле агента, держа поток UI занятым.

    UI-поток здесь синхронно ждёт future (только в тесте — чтобы UI точно
    не успел обработать вызов). Блокирующий мост `call_from_thread` встал
    бы до обработки UI — future не дождались бы. Неблокирующий отпускает
    цикл сразу. Возвращает, успел ли цикл."""

    async def _on_loop() -> bool:
        call()
        return True

    future = loop.submit(_on_loop())
    try:
        return future.result(timeout=2.0)
    except concurrent.futures.TimeoutError:
        return False


async def test_stream_signal_does_not_block_the_loop(app_pilot_with_loop):
    app = app_pilot_with_loop.app
    loop = app_pilot_with_loop.agent_loop
    drawn = []
    app._stream_signal = lambda: drawn.append(True)
    assert _hold_ui(loop, app._post_stream_signal)
    assert drawn == []  # UI ещё не обработал — а цикл уже свободен
    await wait_until(app_pilot_with_loop.pilot, lambda: drawn, "кадр стрима")


def test_stream_pump_signals_without_blocking(app_on_loop):
    app_on_loop.rebuild_ctx()
    app = app_on_loop.app
    assert app._stream_pump._signal == app._post_stream_signal


async def test_runtime_notice_does_not_block_the_loop(app_pilot_with_loop):
    app = app_pilot_with_loop.app
    loop = app_pilot_with_loop.agent_loop
    refreshed = []
    app.refresh_notices_indicators = lambda: refreshed.append(True)
    assert _hold_ui(loop, app._on_runtime_notice)
    assert refreshed == []
    await wait_until(
        app_pilot_with_loop.pilot, lambda: refreshed, "счётчики обновлены"
    )


async def test_runtime_notice_on_the_ui_thread_refreshes(app_pilot_with_loop):
    """С потока UI уведомление тоже доходит (раньше `call_from_thread`
    здесь бросал и счётчики не обновлялись)."""
    app = app_pilot_with_loop.app
    refreshed = []
    app.refresh_notices_indicators = lambda: refreshed.append(True)
    app._on_runtime_notice()
    await wait_until(
        app_pilot_with_loop.pilot, lambda: refreshed, "счётчики обновлены"
    )


class _CancelledCompact:
    async def acompact_thread(self, thread_id, system=None):
        raise asyncio.CancelledError()

    def stop_run(self) -> None:
        pass


def _capture_thread_errors(monkeypatch) -> list:
    errors: list = []
    monkeypatch.setattr(
        threading, "excepthook", lambda args: errors.append(args.exc_type)
    )
    return errors


def test_cancelled_compact_is_not_a_runner_error(agent_loop, monkeypatch):
    thread_errors = _capture_thread_errors(monkeypatch)
    errors: list = []
    runner = AgentRunner(
        executor=_CancelledCompact(), loop=agent_loop, on_error=errors.append
    )
    assert runner.compact_now("t1", "cli")
    runner.wait(5.0)
    assert errors == []
    assert thread_errors == []


def test_cancelled_warm_build_is_not_a_runner_error(agent_loop, monkeypatch):
    thread_errors = _capture_thread_errors(monkeypatch)
    errors: list = []
    built = threading.Event()

    def _factory():
        built.set()
        raise concurrent.futures.CancelledError()

    runner = AgentRunner(
        executor_factory=_factory, loop=agent_loop, on_error=errors.append
    )
    runner.warm()
    assert built.wait(5.0)
    for thread in [t for t in threading.enumerate() if t.name == "gc-ua-warm"]:
        thread.join(5.0)
    assert errors == []
    assert thread_errors == []


def test_shutdown_lets_cleanup_born_of_cancel_finish(agent_loop):
    """Уборка, родившаяся при отмене отдельной задачей (её нет в первом
    снимке задач цикла), доигрывает до закрытия цикла, а не бросается."""
    started = threading.Event()
    closed: list[str] = []

    async def _close() -> None:
        await asyncio.sleep(0.3)
        closed.append("closed")

    async def _turn() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            asyncio.get_running_loop().create_task(_close())

    agent_loop.submit(_turn())
    assert started.wait(5)
    agent_loop.shutdown()

    assert closed == ["closed"]


def test_shutdown_cancels_cleanup_past_the_budget(agent_loop):
    """Уборка, не уложившаяся в срок выхода, отменяется (закрытие сессии на
    отмене добивает процесс), и поток цикла выходит в срок."""
    started = threading.Event()
    seen: list[str] = []

    async def _stuck_close() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            seen.append("cancelled")
            raise

    async def _turn() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.shield(_stuck_close())

    agent_loop.submit(_turn())
    assert started.wait(5)
    agent_loop.shutdown(timeout=2.0)

    assert seen == ["cancelled"]
    assert not agent_loop.thread.is_alive()


#: stdio-сервер с медленным закрытием: глух к концу stdin и к SIGTERM —
#: закрытие сессии не укладывается в срок выхода, выходит только по SIGKILL.
_SLOW_CLOSE_SERVER = r"""
import json, signal, sys, time

signal.signal(signal.SIGTERM, signal.SIG_IGN)

for line in sys.stdin:
    req = json.loads(line or "{}")
    if req.get("id") is not None:
        sys.stdout.write(json.dumps(
            {"jsonrpc": "2.0", "id": req["id"], "result": {}}) + "\n")
        sys.stdout.flush()
time.sleep(60)
"""


@pytest.mark.skipif(os.name != "posix", reason="сигналы процессов — POSIX")
def test_exit_mid_turn_leaves_no_persistent_mcp_process(agent_loop, tmp_path):
    """Выход посреди хода: процесс постоянной stdio-сессии MCP с медленным
    закрытием не переживает цикл агента (проверка по PID)."""
    import sys

    from gc_core.tools.connect_mcp import persistent_stdio as ps

    script = tmp_path / "slow_close_server.py"
    script.write_text(_SLOW_CLOSE_SERVER, encoding="utf-8")
    session = ps.PersistentStdioSession(
        {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
            "persistent_session": True,
        }
    )
    session.call("tools/call", {"name": "ping", "arguments": {}}, timeout=10)
    pid = session._proc.pid
    started = threading.Event()

    async def _turn() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.shield(session.aclose())

    agent_loop.submit(_turn())
    assert started.wait(5)
    agent_loop.shutdown(timeout=3.0)

    assert _exits_within(pid, 2.0)


def _exits_within(pid: int, timeout: float) -> bool:
    """Дочерний процесс вышел за ``timeout`` (SIGKILL доходит не мгновенно);
    вышедшего пожинает."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if os.waitpid(pid, os.WNOHANG) != (0, 0):
                return True
        except ChildProcessError:  # уже пожат — вышел
            return True
        time.sleep(0.05)
    return False
