"""Переход из сессии с идущим ходом (решение 25): только через вопрос
«остановить и перейти». Остаться — ничего не меняется; остановить — ход
кончается, его исход ложится в его тред, и только потом переход."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("textual")

from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.screens.confirm import ConfirmScreen
from gc_tui.screens.sessions import SessionsScreen

_A = "gate0000000000000000000000000000a"
_B = "gate0000000000000000000000000000b"
_QUESTION = "В этой сессии идёт ход. Остановить его и перейти?"


def _write(tmp_path, tid):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _status(tmp_path, tid):
    path = tmp_path / f"meta_cli_{tid}.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    return (meta.get("tui") or {}).get("status")


class _Runner:
    """Ход треда `tid`. `slow` — стоп кооперативный: поток ещё жив, пока
    тест не «доведёт» ход до конца."""

    _thread_kind = "run"

    def __init__(self, tid, *, slow=False):
        self._last_target = (tid, "cli")
        self._stop_requested = False
        self.run_started_at = None
        self.running = True
        self.slow = slow
        self.stops = 0

    def is_running(self):
        return self.running

    def stop(self):
        self.stops += 1
        self._stop_requested = True
        if not self.slow:
            self.running = False

    def cancel_confirm(self):
        pass

    def cancel_ask_human(self):
        pass

    def withdraw_message(self, msg_id):
        return False


def _app(tmp_path):
    _write(tmp_path, _A)
    _write(tmp_path, _B)
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


async def _run_in_a(pilot, *, slow=False):
    scr = pilot.app.screen
    scr.attach_active_task(_A, is_resume=True)
    scr._prepare_for_new_run()
    runner = _Runner(_A, slow=slow)
    pilot.app.active_runner = runner
    scr._poll_active()  # тик застал ход
    await pilot.pause()
    return scr, runner


def _dialog(pilot):
    found = [s for s in pilot.app.screen_stack if isinstance(s, ConfirmScreen)]
    return found[0] if found else None


async def _pick_b(pilot, tmp_path):
    """/sessions → выбрать B (Enter по карточке)."""
    await pilot.app.push_screen(
        SessionsScreen(threads_dir=str(tmp_path), system="cli")
    )
    await pilot.pause()
    pilot.app.screen._resume_thread(_B)
    await pilot.pause()


def _new(scr):
    scr._cmd_new()


async def _switch(pilot, tmp_path, path):
    if path == "new":
        _new(pilot.app.screen)
    else:
        await _pick_b(pilot, tmp_path)
    await pilot.pause()


_TARGET = {"new": None, "sessions": _B}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "sessions"])
async def test_no_run_switches_at_once(tmp_path, path):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_A, is_resume=True)
        await _switch(pilot, tmp_path, path)
        assert _dialog(pilot) is None
        assert scr._active_task_id == _TARGET[path]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "sessions"])
async def test_run_asks_and_stay_changes_nothing(tmp_path, path):
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot)
        await _switch(pilot, tmp_path, path)
        dialog = _dialog(pilot)
        assert dialog is not None
        assert dialog._message == _QUESTION
        assert [label for _key, label in dialog._options] == [
            "Остановить и перейти",
            "Остаться",
        ]
        await pilot.press("n")
        await pilot.pause()
        scr._poll_active()
        assert scr._active_task_id == _A
        assert runner.stops == 0
        assert runner.is_running()
        assert pilot.app.active_runner is runner
        assert _status(tmp_path, _A) == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "sessions"])
async def test_stop_switches_at_once_while_old_worker_lingers(tmp_path, path):
    """Стоп кооперативный: поток прежнего хода ещё жив, а переход — сразу.
    Исход «stopped» уже в A; второй переход — обычный, без вопроса."""
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot, slow=True)
        await _switch(pilot, tmp_path, path)
        await pilot.press("y")
        await pilot.pause()
        assert runner.stops == 1
        assert runner.is_running()  # поток дорабатывает в фоне
        assert scr._active_task_id == _TARGET[path]
        assert _status(tmp_path, _A) == "stopped"
        scr._poll_active()
        await pilot.pause()
        assert _dialog(pilot) is None
        scr.request_thread_switch(_A)
        await pilot.pause()
        assert _dialog(pilot) is None
        assert scr._active_task_id == _A


@pytest.mark.asyncio
async def test_plain_stop_then_new_switches_at_once(tmp_path):
    """После обычного стопа поток хода ещё жив — /new не ждёт его."""
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot, slow=True)
        scr._events_since_last_user_send = 1  # не откат
        scr._cmd_stop()
        await pilot.pause()
        assert runner.is_running()
        _new(scr)
        await pilot.pause()
        assert _dialog(pilot) is None
        assert scr._active_task_id is None


@pytest.mark.asyncio
async def test_late_events_of_stopped_run_stay_out_of_new_thread(tmp_path):
    """Поздние события остановленного хода: ни новый тред, ни его мета их
    не видят — поток прошлого хода отвязан от окна."""
    from gc_core.execution.progress import QueueSubagentProgressSink
    from gc_tui.services.stream_pump import StreamPump

    async with _app(tmp_path).run_test() as pilot:
        app = pilot.app
        scr, runner = await _run_in_a(pilot, slow=True)
        # как после сборки исполнителя хода A
        pump = StreamPump(app._stream_sink, signal=lambda: None, throttle_s=0)
        app._stream_pump = pump
        app._subagent_progress_sink = QueueSubagentProgressSink()
        app.load_circuit_breaker({"m-a": "tripped"})
        await _pick_b(pilot, tmp_path)
        await pilot.press("y")
        await pilot.pause()
        assert scr._active_task_id == _B
        app.load_circuit_breaker({"m-b": "tripped"})  # предохранитель B
        # поток A дорабатывает: дельты стрима, прогресс, итог предохранителя
        pump.on_delta("answer", "поздний текст A")
        assert not app._stream_sink.has_content()
        assert app._subagent_progress_sink is None
        app._persist_breaker_after_worker(runner)
        a_tui = json.loads(
            (tmp_path / f"meta_cli_{_A}.json").read_text(encoding="utf-8")
        )["tui"]
        assert a_tui["circuit_breaker"] == {"m-a": "tripped"}
        assert _status(tmp_path, _B) is None
        # новый ход в B — вопрос сам не всплывает
        app.active_runner = _Runner(_B)
        scr._poll_active()
        await pilot.pause()
        assert _dialog(pilot) is None


@pytest.mark.asyncio
async def test_resume_from_preview_goes_through_gate(tmp_path):
    """Превью → «продолжить»: тот же вопрос."""
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot)
        await pilot.app.push_screen(
            SessionsScreen(threads_dir=str(tmp_path), system="cli")
        )
        await pilot.pause()
        sessions = pilot.app.screen
        sessions._resume_thread(_B)
        await pilot.pause()
        assert _dialog(pilot) is not None
        await pilot.press("y")
        await pilot.pause()
        scr._poll_active()
        await pilot.pause()
        assert scr._active_task_id == _B
        assert _status(tmp_path, _A) == "stopped"


@pytest.mark.asyncio
async def test_stop_and_switch_clears_question_of_old_run(tmp_path):
    """Ход ждал ответа человека: после перехода вопроса прежней сессии нет."""
    from gc_tui.state import AskHumanState

    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot)
        from textual.widgets import Static

        banner = Static("?")
        await scr.mount(banner)
        scr._ask_human_banner = banner
        pilot.app.tui_state.ask_human_state = AskHumanState(
            question="?", call_id="c1"
        )
        _new(scr)
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        scr._poll_active()
        await pilot.pause()
        assert scr._active_task_id is None
        assert pilot.app.tui_state.ask_human_state is None


@pytest.mark.asyncio
async def test_stop_and_switch_clears_confirm_of_old_run(tmp_path):
    """Ход ждал подтверждения: баннер прежней сессии в новую не едет."""
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot)
        scr._confirm_active = True
        await _pick_b(pilot, tmp_path)
        await pilot.press("y")
        await pilot.pause()
        scr._poll_active()
        await pilot.pause()
        assert scr._active_task_id == _B
        assert scr._confirm_active is False


@pytest.mark.asyncio
async def test_new_thread_line_does_not_blink_for_old_stop(tmp_path):
    """Мигающий «■» — строка останавливаемого хода своей сессии; в
    открытой после перехода сессии итог её собственный."""
    app = _app(tmp_path)
    from gc_core.persistence.turn_outcome import TurnOutcome
    from gc_core.thread_store import (
        append_new_thread_messages,
        append_turn_outcome_record,
    )

    path = tmp_path / f"meta_cli_{_B}.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["tui"] = {"status": "completed"}
    path.write_text(json.dumps(meta), encoding="utf-8")
    # Свой закрытый ход B: реплика и запись итога ядра «за 3с».
    append_new_thread_messages(
        str(tmp_path),
        "cli",
        _B,
        [{"id": "u1", "role": "user", "content": "q"}],
        set(),
    )
    append_turn_outcome_record(
        str(tmp_path),
        "cli",
        _B,
        TurnOutcome(
            status="done",
            reason=None,
            opened_by="u1",
            started_at="2026-10-22T11:32:00Z",
            ended_at="",
            duration_sec=3.0,
            error=None,
        ),
        seq=1,
    )
    async with app.run_test() as pilot:
        scr, runner = await _run_in_a(pilot, slow=True)
        await _pick_b(pilot, tmp_path)
        await pilot.press("y")
        await pilot.pause()
        scr._poll_active()
        await pilot.pause()
        assert scr._active_task_id == _B
        assert scr._thinking._render_text().plain == "✓ Выполнил за 3с"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "sessions"])
async def test_switch_forgets_undo_of_previous_send(tmp_path, path):
    """Откат по Esc — последней отправки этого вида; после перехода её нет:
    ход новой сессии, начатый без отправки (утверждение плана), не снимает
    её реплику."""
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_A, is_resume=True)
        scr._last_user_message_id = "m_a"
        scr._events_since_last_user_send = 0  # отправка только что в A
        await _switch(pilot, tmp_path, path)
        assert scr._active_task_id == _TARGET[path]
        assert scr._last_user_message_id is None
        assert scr._can_cancel_with_undo() is False


@pytest.mark.asyncio
async def test_stop_then_busy_target_says_busy(tmp_path):
    """Цель успели занять другим окном: ход уже остановлен, переход не
    состоялся — человек видит «сессия занята», а не тишину."""
    from gc_tui.services.session_lock import BUSY_MESSAGE, try_acquire

    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot)
        notes = []
        pilot.app.notify = lambda msg, **kw: notes.append(msg)
        assert try_acquire(str(tmp_path), "cli", _B, "other-tui")
        scr.request_thread_switch(_B)
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert scr._active_task_id == _A
        assert BUSY_MESSAGE in notes
        assert _status(tmp_path, _A) == "stopped"


@pytest.mark.asyncio
async def test_late_trip_of_stopped_run_stays_out_of_window_breaker(tmp_path):
    """Предохранитель окна после стопа — свой объект: поздний трип
    отвязанного хода A не попадает ни в предохранитель окна, ни в мету B."""
    async with _app(tmp_path).run_test() as pilot:
        app = pilot.app
        scr, runner = await _run_in_a(pilot, slow=True)
        old = app._circuit_breaker  # его держит исполнитель хода A
        await _pick_b(pilot, tmp_path)
        await pilot.press("y")
        await pilot.pause()
        assert scr._active_task_id == _B
        old.trip("m-a", "поздний сбой")
        assert app._circuit_breaker.snapshot() == {}
        app.persist_circuit_breaker(_B)
        b_tui = json.loads(
            (tmp_path / f"meta_cli_{_B}.json").read_text(encoding="utf-8")
        )["tui"]
        assert b_tui.get("circuit_breaker") == {}
