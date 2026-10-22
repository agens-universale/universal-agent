"""Ход останавливается (решение 26): строка итога — один мигающий `■` без
текста в ритме мигания вызова; ход кончился — ровный `■ Отменено`."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("textual")

from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.services.trace_glyphs import BLINK_PERIOD_TICKS
from gc_tui.services.turn_outcome import OUTCOMES

_A = "stop0000000000000000000000000000a"
_GLYPH = OUTCOMES["cancelled"].glyph
_DONE_LINE = f"{_GLYPH} {OUTCOMES['cancelled'].sentence}"


def _write(tmp_path, tid):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


class _SlowRunner:
    """Стоп кооперативный: поток жив, пока тест не доведёт ход до конца."""

    _thread_kind = "run"

    def __init__(self, tid):
        self._last_target = (tid, "cli")
        self._stop_requested = False
        self.run_started_at = None
        self.running = True

    def is_running(self):
        return self.running

    def stop(self):
        self._stop_requested = True

    def cancel_confirm(self):
        pass

    def cancel_ask_human(self):
        pass

    def withdraw_message(self, msg_id):
        return False


def _app(tmp_path):
    _write(tmp_path, _A)
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


async def _run_in_a(pilot):
    scr = pilot.app.screen
    scr.attach_active_task(_A, is_resume=True)
    scr._prepare_for_new_run()
    scr._reset_thinking_timer()
    runner = _SlowRunner(_A)
    pilot.app.active_runner = runner
    scr._events_since_last_user_send = 1  # агент уже ответил: не откат
    scr._poll_active()
    await pilot.pause()
    return scr, runner


def _frames(ind):
    """Строка итога на целом периоде мигания."""
    seen = []
    for _ in range(BLINK_PERIOD_TICKS):
        ind._tick()
        seen.append(ind._render_text().plain)
    return seen


def _assert_blinking_glyph_only(scr):
    """Тики экрана между кадрами мигания ритм не сбивают."""
    frames = []
    for _ in range(BLINK_PERIOD_TICKS):
        scr._poll_active()
        scr._thinking._tick()
        frames.append(scr._thinking._render_text().plain)
    assert set(frames) == {_GLYPH, " "}


@pytest.mark.asyncio
async def test_stop_blinks_glyph_until_run_ends(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot)
        scr._cmd_stop()
        await pilot.pause()
        _assert_blinking_glyph_only(scr)
        runner.running = False  # ход дошёл до конца
        scr._poll_active()
        await pilot.pause()
        assert set(_frames(scr._thinking)) == {_DONE_LINE}
