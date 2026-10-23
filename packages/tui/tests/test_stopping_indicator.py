"""Ход останавливается (решение 26, S10 §4.4): пока поток стопа жив — бар
с одним мигающим `■` без текста в ритме мигания вызова, запись стопа ждёт
за баром; поток вышел — бар уходит, а строка «■ Отменено · ЧЧ:ММ» встаёт
в ленте тем же движением. Один ход — одна строка."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

pytest.importorskip("textual")

from gc_core.persistence.turn_outcome import USER_STOP, TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_turn_outcome_record,
)
from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.services.relative_time import parse_iso_utc
from gc_tui.services.trace_glyphs import BLINK_PERIOD_TICKS
from gc_tui.services.turn_outcome import OUTCOMES

_A = "stop0000000000000000000000000000a"
_B = "stop0000000000000000000000000000b"
_GLYPH = OUTCOMES["cancelled"].glyph
_CANCELLED = f"{_GLYPH} {OUTCOMES['cancelled'].sentence}"
_ENDED = "2026-10-22T11:30:12Z"
_MID = "m_stopped"


@pytest.fixture(autouse=True)
def _pin_now(monkeypatch):
    """«Сейчас» — минута после стопа: время без даты."""
    from gc_tui.services import relative_time

    now = parse_iso_utc(_ENDED).astimezone() + timedelta(minutes=1)
    monkeypatch.setattr(relative_time, "_now_local", lambda: now)


def _stopped_line() -> str:
    hm = parse_iso_utc(_ENDED).astimezone().strftime("%H:%M")
    return f"{_CANCELLED} · {hm}"


def _write(tmp_path, tid):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _stop_record(tmp_path, tid=_A):
    """Поток стопа дописал ход: реплику и запись стопа, как ядро."""
    append_new_thread_messages(
        str(tmp_path),
        "cli",
        tid,
        [{"id": _MID, "role": "user", "content": "долгая задача"}],
        set(),
    )
    outcome = TurnOutcome(
        status="stopped",
        reason=USER_STOP,
        opened_by=_MID,
        started_at="2026-10-22T11:30:00Z",
        ended_at=_ENDED,
        duration_sec=12.0,
        error=None,
    )
    append_turn_outcome_record(str(tmp_path), "cli", tid, outcome, seq=1)


class _SlowRunner:
    """Стоп кооперативный: поток жив, пока тест не доведёт ход до конца.
    `on_stop` — что поток успевает до выхода сразу на стопе."""

    _thread_kind = "run"

    def __init__(self, tid, *, on_stop=None):
        self._last_target = (tid, "cli")
        self._stop_requested = False
        self.running = True
        self.run_seq = 1
        self._on_stop = on_stop

    def is_running(self):
        return self.running

    def stop(self):
        self._stop_requested = True
        if self._on_stop is not None:
            self._on_stop(self)

    def cancel_confirm(self):
        pass

    def cancel_ask_human(self):
        pass

    def withdraw_message(self, msg_id):
        return False

    def follow(self, _other):
        pass

    def turn_effective_start(self):
        return None


def _app(tmp_path):
    _write(tmp_path, _A)
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


async def _start_in_a(pilot, *, seen=True, **kw):
    """Ход в треде A. `seen=False` — стоп раньше первого тика, увидевшего
    прогон."""
    scr = pilot.app.screen
    scr.attach_active_task(_A, is_resume=True)
    runner = _SlowRunner(_A, **kw)
    pilot.app.active_runner = runner
    scr._prepare_for_new_run()
    scr._reset_thinking_timer()
    scr._last_user_message_id = None  # без отката: ход что-то выдал
    if seen:
        scr._poll_active()
        await pilot.pause()
    return scr, runner


def _bar_up(scr) -> bool:
    return scr.query_one("#indicator-bar").has_class("-turn")


def _lines(scr):
    from gc_tui.widgets.turn_line import TurnLine

    return list(scr.query_one("#trace").query(TurnLine))


def _assert_blinking_glyph_only(scr):
    """Тики экрана между кадрами мигания ритм не сбивают."""
    frames = []
    for _ in range(BLINK_PERIOD_TICKS):
        scr._poll_active()
        scr._thinking._tick()
        frames.append(scr._thinking._render_text().plain)
    assert set(frames) == {_GLYPH, " "}


async def _assert_stop_line_after_exit(pilot, scr, runner):
    """Поток стопа вышел: бар снят, одна строка стопа видна свежей."""
    runner.running = False
    scr._poll_active()
    await pilot.pause()
    assert not _bar_up(scr)
    assert scr._thinking is None
    (line,) = _lines(scr)
    assert line.text.plain == _stopped_line()
    assert line.is_fresh
    assert line.display


@pytest.mark.asyncio
async def test_stop_blinks_glyph_until_run_ends(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _start_in_a(pilot)
        scr._cmd_stop()
        await pilot.pause()
        assert _bar_up(scr)
        assert scr._thinking.state == "stopping"
        _assert_blinking_glyph_only(scr)
        _stop_record(tmp_path)
        scr._poll_active()  # запись стопа дочитана, поток ещё жив
        (line,) = _lines(scr)
        assert line.has_class("-held")
        assert _bar_up(scr)
        await _assert_stop_line_after_exit(pilot, scr, runner)


@pytest.mark.asyncio
async def test_stop_before_first_tick_gives_one_line(tmp_path):
    """Стоп раньше первого тика, увидевшего прогон: прогон стопа — тот,
    что дорабатывает, а не сменный раннер. Строка без записи не встаёт,
    пока поток жив; запись стопа даёт одну строку."""
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _start_in_a(pilot, seen=False)
        scr._cmd_stop()
        for _ in range(3):
            scr._poll_active()
        await pilot.pause()
        assert _lines(scr) == []
        assert _bar_up(scr)
        _stop_record(tmp_path)
        await _assert_stop_line_after_exit(pilot, scr, runner)


@pytest.mark.asyncio
async def test_instant_stop_shows_line_in_same_call(tmp_path):
    """Мгновенный стоп (поток вышел к моменту показа): строка из записи
    стопа и уход бара — в том же вызове, без тика."""

    def exit_with_record(runner):
        _stop_record(tmp_path)
        runner.running = False

    async with _app(tmp_path).run_test() as pilot:
        scr, _runner = await _start_in_a(pilot, on_stop=exit_with_record)
        scr._cmd_stop()
        assert not _bar_up(scr)
        assert scr._thinking is None
        (line,) = _lines(scr)
        assert line.text.plain == _stopped_line()
        assert line.is_fresh


@pytest.mark.asyncio
async def test_instant_stop_without_record_shows_fallback_line(tmp_path):
    """Мгновенный стоп, записи нет: запасная «■ Отменено» без времени."""

    def exit_now(runner):
        runner.running = False

    async with _app(tmp_path).run_test() as pilot:
        scr, _runner = await _start_in_a(pilot, on_stop=exit_now)
        scr._cmd_stop()
        assert not _bar_up(scr)
        (line,) = _lines(scr)
        assert line.text.plain == _CANCELLED
        assert not line.recorded


@pytest.mark.asyncio
async def test_stop_for_ask_human_same_outcome(tmp_path):
    """Esc на вопросе агента: баннер снят, дальше путь стопа — мигание,
    затем строка «■ Отменено · ЧЧ:ММ» и уход бара одним движением."""
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _start_in_a(pilot)
        scr._show_child_ask_human(parent_call_id="p", prose="Какой путь?")
        await pilot.pause()
        scr._stop_for_ask_human()
        await pilot.pause()
        assert scr._ask_human_banner is None
        assert _bar_up(scr)
        assert scr._thinking.state == "stopping"
        _stop_record(tmp_path)
        await _assert_stop_line_after_exit(pilot, scr, runner)


@pytest.mark.asyncio
async def test_stop_then_switch_and_back(tmp_path):
    """Стоп и переключение: у открытой сессии бара нет; поток стопа
    дописал своё в прежнюю — при возврате строка стопа на месте."""
    _write(tmp_path, _B)
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _start_in_a(pilot)
        scr._cmd_stop()
        scr.attach_active_task(_B, is_resume=True)
        scr._poll_active()
        await pilot.pause()
        assert not _bar_up(scr)
        assert _lines(scr) == []
        _stop_record(tmp_path)
        runner.running = False
        scr._poll_active()
        scr.attach_active_task(_A, is_resume=True)
        await pilot.pause()
        (line,) = _lines(scr)
        assert line.text.plain == _stopped_line()
        assert line.is_fresh


@pytest.mark.asyncio
async def test_next_run_outcome_not_covered_by_old_stop_blink(tmp_path):
    """Старый поток ещё дорабатывает, а новый ход того же треда кончился:
    строка — итог нового хода, не мигание прошлого стопа."""
    async with _app(tmp_path).run_test() as pilot:
        scr, old = await _start_in_a(pilot)
        scr._cmd_stop()
        await pilot.pause()
        assert old.is_running()  # поток стопа ещё жив
        new = _SlowRunner(_A)
        pilot.app.active_runner = new
        scr._prepare_for_new_run()
        scr._reset_thinking_timer()
        scr._poll_active()
        new.running = False  # новый ход кончился сам
        scr._poll_active()
        await pilot.pause()
        assert old.is_running()
        assert not _bar_up(scr)
        fresh = [line for line in _lines(scr) if line.is_fresh]
        assert [line.text.plain for line in fresh] == ["✓ Выполнил"]
