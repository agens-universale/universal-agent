"""Приёмка хоста без субагентов: один цикл агента на приложение (B′1, T2).

Смена хоста касается всех: ход больше не поднимает свой ``asyncio.run`` в
свежем потоке, а идёт корутиной на общем долгоживущем цикле. Каждый
сценарий здесь — настоящее окно (``run_test``), настоящий раннер из
``get_runner`` и исполнитель-двойник на портах хоста: ход — корутина на
цикле агента, тул и подтверждение — в потоке (как гард ядра), вопрос
человеку — через порт ``ctx.ask_human``, стоп кооперативный.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from gc_testkit.tui_support import tui_settings, wait_until

_LOOP_THREAD = "gc-ua-agent-loop"


class _HostExecutor:
    """Двойник `AgentExecutor`: шаги хода берёт из плана фабрики."""

    def __init__(self, factory: _Factory) -> None:
        self._factory = factory
        self._ctx = SimpleNamespace(ask_human=None, tui_confirm=None)
        runner = factory.app.active_runner
        self._ctx.tui_confirm = runner.make_tui_confirm(
            factory.app._present_confirm
        )
        self.turns: list[list[str]] = []
        self.loops: list[asyncio.AbstractEventLoop] = []
        self.threads: list[str] = []
        self.answers: list = []
        self.choices: list[str] = []
        self.stdin_reads: list[str] = []
        self.compacted_on: list[str] = []
        self.queued: list[dict] = []
        self.outcomes: list[str] = []
        self.last_terminal_outcome: str | None = None
        self.obey_stop = factory.obey_stop
        self.stopped = False
        self.tool_started = threading.Event()
        self.asking = threading.Event()
        self.reading = threading.Event()
        self._wake = threading.Event()
        self._state: object = object()

    # --- ход ---------------------------------------------------------
    async def arun(self, messages, *, thread_id, **kw) -> None:
        self.turns.append(list(messages))
        self.loops.append(asyncio.get_running_loop())
        self.threads.append(threading.current_thread().name)
        for step in self._factory.next_steps():
            await getattr(self, f"_step_{step}")()
            if self.stopped and self.obey_stop:
                self._finish("stopped")
                return
        self._finish("stopped" if self.stopped else "completed")

    def _finish(self, outcome: str) -> None:
        self.last_terminal_outcome = outcome
        self.outcomes.append(outcome)

    async def _step_tool(self) -> None:
        self._state = SimpleNamespace(produced_output=True)
        self.tool_started.set()
        await asyncio.to_thread(self._wake.wait, 30)

    async def _step_confirm(self) -> None:
        choice = await asyncio.to_thread(
            self._ctx.tui_confirm,
            title="Запись файла",
            question="Записать файл?",
            fields=[],
        )
        self.choices.append(choice)

    async def _step_ask(self) -> None:
        self.asking.set()
        self.answers.append(await self._ctx.ask_human("Какой цвет?"))

    async def _step_input(self) -> None:
        # Чужой код, читающий stdin прямо в корутине, — на потоке цикла.
        self.reading.set()
        self.stdin_reads.append(input())

    async def _step_hang(self) -> None:
        await asyncio.Event().wait()  # стоп не слушает

    async def acompact_thread(self, thread_id, system=None) -> bool:
        self.compacted_on.append(threading.current_thread().name)
        return True

    # --- порты раннера ----------------------------------------------
    def release_tool(self) -> None:
        self._wake.set()

    def stop_run(self) -> None:
        self.stopped = True
        if self.obey_stop:
            self._wake.set()

    def add_message(self, message: dict) -> None:
        self.queued.append(message)

    def take_undelivered_messages(self) -> list[dict]:
        taken, self.queued = self.queued, []
        return taken

    def withdraw_message(self, msg_id: str) -> bool:
        return False

    def get_state(self) -> object:
        return self._state


class _Factory:
    """Фабрика исполнителей окна: каждый ход берёт свой список шагов."""

    def __init__(self, app, plan: list[list[str]], obey_stop: bool) -> None:
        self.app = app
        self.plan = list(plan)
        self.obey_stop = obey_stop
        self.built: list[_HostExecutor] = []

    def __call__(self) -> _HostExecutor:
        executor = _HostExecutor(self)
        self.built.append(executor)
        return executor

    def next_steps(self) -> list[str]:
        return self.plan.pop(0) if self.plan else []


def _settings():
    return tui_settings(
        {"agent.model": "M", "backend.api_mode": "chat_completions"}
    )


def _make_app(tmp_path, plan, obey_stop=True):
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", settings=_settings())
    factory = _Factory(app, plan, obey_stop)
    app._executor_factory = factory
    return app, factory


class _Host:
    def __init__(self, app, pilot, factory: _Factory) -> None:
        self.app = app
        self.pilot = pilot
        self.factory = factory

    @property
    def executor(self) -> _HostExecutor:
        return self.factory.built[-1]

    async def send(self, text: str) -> None:
        inp = self.app.query_one("#cmdinput")
        inp.text = text
        await self.pilot.press("enter")

    async def until(self, cond, what: str) -> None:
        await wait_until(self.pilot, cond, what)

    async def until_idle(self) -> None:
        await self.until(self._idle, "раннер свободен")

    def _idle(self) -> bool:
        runner = self.app.active_runner
        return runner is None or not runner.is_running()

    async def esc_stop(self, executor: _HostExecutor) -> None:
        """Esc посреди хода: первый — подсказка «стоп», второй — стоп."""
        await self.pilot.press("escape")
        await self.pilot.pause()
        if not executor.stopped:
            await self.pilot.press("escape")
        await self.until(lambda: executor.stopped, "стоп дошёл до хода")

    async def loop_tasks(self) -> list:
        loop = self.app.agent_loop
        return await asyncio.wrap_future(loop.submit(_other_tasks()))


async def _other_tasks() -> list:
    me = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not me]


def _host_fixture(plan, obey_stop=True):
    @pytest.fixture
    async def _fixture(tmp_path):
        app, factory = _make_app(tmp_path, plan, obey_stop)
        async with app.run_test() as pilot:
            yield _Host(app, pilot, factory)

    return _fixture


host = _host_fixture([[]])
host_tool = _host_fixture([["tool"], []])
host_confirm = _host_fixture([["confirm"]])
host_ask = _host_fixture([["ask"]])
host_input = _host_fixture([["input"]])
host_stubborn = _host_fixture([["tool"], []], obey_stop=False)


async def test_plain_turn(host):
    await host.send("привет")
    await host.until(lambda: host.executor.outcomes, "ход закончился")
    await host.until_idle()
    ex = host.executor
    assert ex.turns == [["привет"]]
    assert ex.threads == [_LOOP_THREAD]
    assert host.app.last_terminal_outcome == "completed"
    assert host.app.last_runner_error is None
    assert await host.loop_tasks() == []


async def test_esc_mid_tool(host_tool):
    h = host_tool
    await h.send("долгий тул")
    first = h.executor
    await h.until(first.tool_started.is_set, "тул начался")
    await h.esc_stop(first)
    await h.until(lambda: first.outcomes == ["stopped"], "ход остановлен")
    await h.until_idle()
    # Стоп снял ход, а не цикл: следующий ход идёт на том же цикле.
    await h.send("дальше")
    await h.until(
        lambda: h.factory.built[-1].outcomes == ["completed"],
        "новый ход прошёл",
    )
    second = h.factory.built[-1]
    assert second is not first
    assert second.loops[0] is first.loops[0]
    assert h.app.agent_loop.thread.is_alive()


async def test_esc_on_confirm(host_confirm):
    h = host_confirm
    await h.send("запиши файл")
    screen = h.app.screen
    await h.until(lambda: screen._confirm_active, "баннер подтверждения")
    await h.pilot.press("escape")
    ex = h.executor
    await h.until(lambda: ex.outcomes, "ход закончился")
    assert ex.choices == ["deny_once"]
    assert ex.outcomes == ["completed"]
    assert not ex.stopped


async def test_ask_human_answer(host_ask):
    h = host_ask
    await h.send("спроси меня")
    ex = h.executor
    await h.until(ex.asking.is_set, "агент спросил")
    h.app.active_runner.submit_ask_human_reply("синий")
    await h.until(lambda: ex.outcomes, "ход закончился")
    assert ex.answers == ["синий"]
    assert ex.outcomes == ["completed"]


async def test_ask_human_cancel(host_ask):
    h = host_ask
    await h.send("спроси меня")
    ex = h.executor
    await h.until(ex.asking.is_set, "агент спросил")
    h.app.active_runner.cancel_ask_human()
    await h.until(lambda: ex.outcomes, "ход закончился")
    assert ex.answers == [""]


async def test_cancel_wakes_foreign_stdin_reader_on_the_loop(host_input):
    """Мост stdin — на потоке цикла: ``input()`` в корутине читает очередь
    приложения, и отмена его будит (иначе встал бы весь цикл)."""
    h = host_input
    await h.send("прочитай stdin")
    ex = h.executor
    await h.until(ex.reading.is_set, "чтение stdin началось")
    h.app.active_runner.cancel_ask_human()
    await h.until(lambda: ex.outcomes, "ход закончился")
    assert ex.stdin_reads == [""]
    assert await h.loop_tasks() == []


async def test_tail_turn_from_queue(host_tool):
    h = host_tool
    await h.send("первое")
    ex = h.executor
    await h.until(ex.tool_started.is_set, "тул начался")
    await h.send("второе")
    await h.until(lambda: ex.queued, "реплика встала в очередь")
    ex.release_tool()
    await h.until(lambda: len(ex.outcomes) == 2, "хвост доигран")
    await h.until_idle()
    assert ex.turns == [["первое"], ["второе"]]
    assert ex.loops[0] is ex.loops[1]


def _write_task(tmp_path, tid: str, n_messages: int) -> None:
    meta = {
        "thread_id": tid,
        "system_id": "cli",
        "runtime": {"live_msg_count": n_messages},
        "tui": {"last_compact_msg_count": 1},
    }
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    rows = [
        json.dumps(
            {
                "type": "message",
                "thread_id": tid,
                "system": "cli",
                "seq": i,
                "ts": "t",
                "payload": {"role": "user", "content": f"m{i}"},
            }
        )
        for i in range(1, n_messages + 1)
    ]
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(rows) + "\n", encoding="utf-8"
    )


async def test_compact(tmp_path):
    tid = "hst00000000000000000000000000001a"
    _write_task(tmp_path, tid, n_messages=3)
    app, factory = _make_app(tmp_path, [[]])
    async with app.run_test() as pilot:
        h = _Host(app, pilot, factory)
        screen = app.screen
        screen.attach_active_task(tid)
        app.get_runner()
        screen._cmd_compact()
        await h.until(lambda: factory.built, "исполнитель собран")
        ex = h.executor
        await h.until(lambda: ex.compacted_on, "сжатие прошло")
        await h.until_idle()
        assert ex.compacted_on == [_LOOP_THREAD]
        await h.send("после сжатия")
        await h.until(lambda: ex.outcomes == ["completed"], "ход прошёл")
        assert ex.threads == [_LOOP_THREAD]


async def test_new_turn_while_stopped_one_finishes(host_stubborn):
    h = host_stubborn
    await h.send("первое")
    old = h.executor
    await h.until(old.tool_started.is_set, "тул начался")
    await h.esc_stop(old)
    assert old.outcomes == []  # остановленный ход ещё дорабатывает
    await h.send("второе")
    await h.until(lambda: len(h.factory.built) == 2, "новый раннер собран")
    new = h.factory.built[-1]
    old.release_tool()
    await h.until(lambda: new.outcomes == ["completed"], "новый ход прошёл")
    await h.until(lambda: old.outcomes == ["stopped"], "старый закрылся")
    assert new.turns == [["второе"]]
    assert new.loops[0] is old.loops[0]


async def test_exit_with_hanging_turn_finishes(tmp_path, monkeypatch):
    """Выход с висящим ходом: вопрос человеку снимает стоп, а ход, глухой к
    стопу, гасит `AgentLoop.shutdown` — процесс не держит ни поток цикла,
    ни поток воркера, ни поток пула с ожиданием ответа.

    Ожидание остановленного хода (`STOP_SETTLE_SEC`) укорочено: ход к стопу
    глух, и ждать его дольше незачем — проверяется исход, не время."""
    monkeypatch.setattr("gc_tui.app.STOP_SETTLE_SEC", 0.01)
    app, factory = _make_app(tmp_path, [["ask", "hang"]])
    async with app.run_test() as pilot:
        h = _Host(app, pilot, factory)
        await h.send("зависни")
        ex = h.executor
        await h.until(ex.asking.is_set, "агент спросил")
        runner = app.active_runner
        loop = app.agent_loop
    assert ex.answers == [""]
    assert not loop.thread.is_alive()
    worker = runner._thread
    worker.join(5.0)
    assert not worker.is_alive()
