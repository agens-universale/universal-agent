"""Индикатор живёт в фиксированной #indicator-bar (вне скролла #trace) —
растущий стрим не может на него наложиться."""

from gc_testkit.tui_support import tui_settings
import json
import threading
from pathlib import Path

import pytest


def _write_task(tmp_path, tid):
    meta = {
        "thread_id": tid,
        "status": "running",
        "updated_at": "2026-06-15T12:00:00+00:00",
        "messages_count": 0,
    }
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


@pytest.mark.asyncio
async def test_indicator_docked_not_in_trace(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator
    from textual.containers import Vertical, VerticalScroll

    tid = "dock1"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        # Индикатор — только у идущей работы: тик покоя его снял бы.
        pilot.app.active_runner = _Run(tid)
        scr.attach_active_task(tid)
        scr._show_thinking_indicator(state="thinking")
        await pilot.pause()
        bar = pilot.app.query_one("#indicator-bar", Vertical)
        trace = pilot.app.query_one("#trace", VerticalScroll)
        assert any(isinstance(w, ThinkingIndicator) for w in bar.children)
        assert not any(
            isinstance(w, ThinkingIndicator) for w in trace.children
        )


@pytest.mark.asyncio
async def test_trace_row_lands_in_trace_with_indicator_docked(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator
    from gc_tui.widgets.trace_row import TraceRow
    from textual.containers import VerticalScroll

    tid = "dock2"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        # Индикатор — только у идущей работы: тик покоя его снял бы.
        pilot.app.active_runner = _Run(tid)
        scr.attach_active_task(tid)
        scr._show_thinking_indicator(state="thinking")
        scr._render_record(
            {
                "payload": {
                    "id": "a1",
                    "role": "assistant",
                    "content": "привет",
                }
            },
            live=True,
        )
        await pilot.pause()
        trace = pilot.app.query_one("#trace", VerticalScroll)
        assert any(
            isinstance(w, TraceRow) for w in trace.children
        )  # строка в трейсе
        assert not any(
            isinstance(w, ThinkingIndicator) for w in trace.children
        )  # индикатор — нет


# --- S10: бар стоит только во время работы открытого треда ------------------


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )


class _Run:
    """Прогон треда `tid`; `seq=None` — раннер без `run_seq` (заглушка)."""

    _thread_kind = "run"

    def __init__(self, tid, *, running=True, seq=1):
        self._last_target = (tid, "cli")
        self.running = running
        if seq is not None:
            self.run_seq = seq

    def is_running(self):
        return self.running

    def stop(self):
        self.running = False

    def turn_effective_start(self):
        return None


def _bar(pilot):
    from textual.containers import Vertical

    return pilot.app.query_one("#indicator-bar", Vertical)


@pytest.mark.asyncio
async def test_bar_absent_when_idle(tmp_path):
    _write_task(tmp_path, "idle1")
    async with _app(tmp_path).run_test() as pilot:
        bar = _bar(pilot)
        assert bar.display is False
        scr = pilot.app.screen
        scr.attach_active_task("idle1")
        scr._poll_active()
        await pilot.pause()
        assert bar.display is False
        assert bar.region.height == 0


@pytest.mark.asyncio
async def test_bar_rule_on_attach(tmp_path):
    for tid in ("ra1", "ra2"):
        _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        bar = _bar(pilot)

        pilot.app.active_runner = _Run("ra1")  # живой прогон треда
        scr.attach_active_task("ra1", is_resume=True)
        await pilot.pause()
        assert bar.display is True
        assert bar.region.height == 2

        pilot.app.active_runner = None  # стоп: поток дорабатывает
        scr._stopping_runner = _Run("ra1")
        scr.attach_active_task("ra1", is_resume=True)
        assert bar.display is True

        # Стоп и переключение: у открытого треда прогона нет.
        scr.attach_active_task("ra2", is_resume=True)
        await pilot.pause()
        assert bar.display is False

        scr._run_pending = True  # ход запущен отсюда, тик его не видел
        scr.attach_active_task("ra2", is_resume=True)
        assert bar.display is True
        scr._run_pending = False


@pytest.mark.asyncio
async def test_bar_on_auto_created_thread(tmp_path):
    """Автосоздание треда под отправку: бар стоит с репликой."""
    _write_task(tmp_path, "auto1")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._prepare_for_new_run()
        scr.attach_active_task("auto1")
        await pilot.pause()
        assert _bar(pilot).display is True


@pytest.mark.asyncio
async def test_bar_rule_tolerates_runner_without_run_seq(tmp_path):
    _write_task(tmp_path, "seq1")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("seq1")
        pilot.app.active_runner = _Run("seq1", seq=None)
        assert scr._bar_wanted() is True
        scr._settled_run = (pilot.app.active_runner, None)
        assert scr._bar_wanted() is False
        pilot.app.active_runner = None


@pytest.mark.asyncio
async def test_trace_ceiling_follows_bar(tmp_path):
    """Потолок #trace пересчитан в том же вызове, где бар показан или
    снят: высота бара — константа, не замер."""
    _write_task(tmp_path, "ceil1")
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("ceil1")
        await pilot.pause()
        idle = scr._trace_max_height
        scr._run_pending = True
        scr._sync_indicator_bar()
        assert scr._trace_max_height == idle - 2
        scr._run_pending = False
        scr._sync_indicator_bar()
        assert scr._trace_max_height == idle


# --- S10 §3.4: сквозной ход — бар 2 строки до спадающего фронта -------------

SCRIPT_TID = "scripted0000000000000000000001a"


def _summary(text):
    return (
        '<technical type="dialog_summary">'
        f"<summary>{text}</summary></technical>"
    )


class _ScriptedExecutor:
    """Ход по шагам. Шаг `i` ждёт `release[i]`; `at` — номер шага, на
    котором ход стоит. Шаги: думаю, стрим, «сверяюсь», «завершаю», вопрос
    агента, подтверждение, запись хода 1 и впуск реплики из очереди,
    запись хода 2 и автосжатие в конце хода (флаг, затем шаг сжатия)."""

    STEPS = 9

    def __init__(self, threads_dir):
        self.dir = str(threads_dir)
        self.release = [threading.Event() for _ in range(self.STEPS)]
        self.at = -1
        self.queued: list[dict] = []
        self.seq = 0

    def _wait(self, step):
        self.at = step
        self.release[step].wait(10)

    def _messages(self, tid, *payloads):
        from gc_core.thread_store import append_new_thread_messages

        append_new_thread_messages(
            self.dir, "cli", tid, list(payloads), set(), start_seq=self.seq
        )
        self.seq += len(payloads)

    def _record(self, tid, opened_by, minute):
        from gc_core.persistence.turn_outcome import TurnOutcome
        from gc_core.thread_store import append_turn_outcome_record

        outcome = TurnOutcome(
            status="done",
            reason=None,
            opened_by=opened_by,
            started_at="2026-10-22T11:30:00Z",
            ended_at=f"2026-10-22T11:3{minute}:12Z",
            duration_sec=12.0,
            error=None,
        )
        append_turn_outcome_record(self.dir, "cli", tid, outcome, seq=self.seq)

    def _flag(self, tid, flag, active):
        from gc_core.thread_store import patch_runtime_flag

        patch_runtime_flag(self.dir, "cli", tid, flag, active=active)

    def _answer(self, text):
        return {
            "id": f"a{self.seq}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }

    async def arun(self, messages, *, thread_id, message_ids=None, **_kw):
        tid = thread_id
        mid = (message_ids or [None])[0]
        self._messages(tid, {"id": mid, "role": "user", "content": "первый"})
        self._wait(0)  # думаю
        self._messages(tid, self._answer("стрим ответа"))
        self._wait(1)
        self._flag(tid, "finish_gate_checking", True)
        self._wait(2)  # сверяюсь
        self._flag(tid, "finish_gate_checking", False)
        self._flag(tid, "finalizing", True)
        self._wait(3)  # завершаю
        self._flag(tid, "finalizing", False)
        self._wait(4)  # вопрос агента — баннер ставит тест
        self._wait(5)  # подтверждение — баннер ставит тест
        self._record(tid, mid, 1)
        nxt = self.queued.pop(0)
        self._messages(
            tid,
            {"id": nxt["id"], "role": "user", "content": nxt["content"]},
            self._answer("ответ второму"),
        )
        self._wait(6)  # ход из очереди впущен внутри прогона
        self._record(tid, nxt["id"], 2)
        self._flag(tid, "summarizing", True)
        self._wait(7)  # сжимаю контекст
        self._compact(tid)
        self._wait(8)  # маркер сжатия в ленте
        self._flag(tid, "summarizing", False)

    def _compact(self, tid):
        from gc_core.thread_store import (
            archive_and_replace_thread_files,
            load_thread_meta,
            save_thread_meta,
        )

        suffix = "20261022_113500"
        meta = load_thread_meta(self.dir, "cli", tid)
        archive_and_replace_thread_files(
            self.dir,
            "cli",
            tid,
            [{"id": "s1", "role": "user", "content": _summary("c")}],
            archive_suffix=suffix,
        )
        save_thread_meta(
            self.dir,
            "cli",
            tid,
            {**meta, "last_summary_archive_suffix": suffix},
        )

    def stop_run(self):
        for event in self.release:
            event.set()

    def turn_effective_start(self):
        return None

    def add_message(self, message):
        self.queued.append(message)

    def withdraw_message(self, msg_id):
        return False

    def take_undelivered_messages(self):
        return []


def _frame(scr):
    """Кадр прошлого тика (его раскладка уже применена): бар, его высота,
    верх поля ввода, видна ли свежая строка итога, видна ли осевшая."""
    from gc_tui.widgets.turn_line import TurnLine

    bar = scr.query_one("#indicator-bar")
    lines = list(scr.query_one("#trace").query(TurnLine))
    return (
        bar.display,
        bar.region.height,
        scr.query_one("#input-row").region.y,
        any(w.display and w.is_fresh for w in lines),
        any(w.display and not w.is_fresh for w in lines),
    )


@pytest.mark.asyncio
async def test_bar_through_scripted_turn(tmp_path):
    from gc_testkit.tui_support import wait_until

    _write_task(tmp_path, SCRIPT_TID)
    ex = _ScriptedExecutor(tmp_path)
    app = _app(tmp_path)
    app._executor_factory = lambda: ex
    frames: list[tuple] = []
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(SCRIPT_TID, is_resume=True)
        tick = scr._poll_tick

        def recorded_tick():
            frames.append(_frame(scr))
            tick()

        scr._poll_tick = recorded_tick
        try:
            inp = pilot.app.query_one("#cmdinput")
            inp.text = "первый"
            await pilot.press("enter")
            for step in range(ex.STEPS):
                await wait_until(pilot, lambda: ex.at == step, f"шаг {step}")
                if step == 3:
                    inp.text = "второй"  # в очередь, пока ход идёт
                    await pilot.press("enter")
                    await wait_until(pilot, lambda: ex.queued, "очередь")
                if step == 4:
                    scr._show_child_ask_human(parent_call_id="p", prose="?")
                if step == 5:
                    scr.show_confirm_banner("Действие", "Разрешить?")
                if step == 8:
                    await wait_until(
                        pilot, lambda: scr._summary_marker_widgets, "маркер"
                    )
                await pilot.pause(0.6)  # пара тиков на шаге
                if step == 4:
                    scr._unmount_ask_human_banner()
                if step == 5:
                    scr._hide_confirm_banner()
                if step == 6:
                    # Впуск из очереди: строка хода 1 осела и видна.
                    assert frames[-1][4] is True
                ex.release[step].set()
            await wait_until(
                pilot,
                lambda: not pilot.app.active_runner.is_running(),
                "прогон вышел",
            )
            await wait_until(pilot, lambda: not frames[-1][0], "бар снят")
            await pilot.pause(0.6)
        finally:
            ex.stop_run()
        edge = next(i for i, f in enumerate(frames) if not f[0])
        assert edge > 0
        for bar_up, height, _y, fresh_shown, _settled in frames[:edge]:
            assert bar_up is True
            assert height == 2
            assert fresh_shown is False
        assert frames[edge][2] == frames[edge - 1][2]
        assert frames[edge][3] is True
        assert all(not f[0] for f in frames[edge:])
