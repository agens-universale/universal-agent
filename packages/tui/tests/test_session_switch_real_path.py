"""«Остановить и перейти» по настоящему пути: клавиши в списке сессий,
Ctrl+N, превью — вопрос отвечается клавишей (y и Enter по умолчанию)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("textual")

from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.screens.confirm import ConfirmScreen
from gc_tui.screens.main import TraceScreen

_A = "real0000000000000000000000000000a"
_B = "real0000000000000000000000000000b"


def _write(tmp_path, tid, updated):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {"thread_id": tid, "system_id": "cli", "updated_at": updated}
        ),
        encoding="utf-8",
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


class _Runner:
    _thread_kind = "run"

    def __init__(self, tid):
        self._last_target = (tid, "cli")
        self._stop_requested = False
        self.running = True
        self.stops = 0

    def is_running(self):
        return self.running

    def stop(self):
        self.stops += 1
        self._stop_requested = True
        self.running = False

    def cancel_confirm(self):
        pass

    def cancel_ask_human(self):
        pass

    def withdraw_message(self, msg_id):
        return False


def _app(tmp_path):
    # B свежее — первая карточка списка
    _write(tmp_path, _A, "2026-10-22T10:00:00+00:00")
    _write(tmp_path, _B, "2026-10-22T11:00:00+00:00")
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


async def _run_in_a(pilot):
    scr = pilot.app.screen
    scr.attach_active_task(_A, is_resume=True)
    scr._prepare_for_new_run()
    runner = _Runner(_A)
    pilot.app.active_runner = runner
    scr._poll_active()
    await pilot.pause()
    return scr, runner


def _confirm(pilot):
    top = pilot.app.screen
    return top if isinstance(top, ConfirmScreen) else None


async def _answer(pilot, how):
    assert _confirm(pilot) is not None
    await pilot.press(how)
    await pilot.pause()
    await pilot.pause()


async def _assert_switched(pilot, scr, runner, target, tmp_path):
    assert isinstance(pilot.app.screen, TraceScreen)
    assert runner.stops == 1
    assert scr._active_task_id == target
    meta = json.loads(
        (tmp_path / f"meta_cli_{_A}.json").read_text(encoding="utf-8")
    )
    assert meta["tui"]["status"] == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["y", "enter"])
async def test_sessions_pick_enter_then_stop_and_switch(tmp_path, how):
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot)
        await pilot.press("f2")
        await pilot.pause()
        sessions = pilot.app.screen
        assert sessions._selected_task().thread_id == _B
        await pilot.press("enter")
        await pilot.pause()
        await _answer(pilot, how)
        await _assert_switched(pilot, scr, runner, _B, tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["y", "enter"])
async def test_ctrl_n_then_stop_and_switch(tmp_path, how):
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot)
        await pilot.press("ctrl+n")
        await pilot.pause()
        await _answer(pilot, how)
        await _assert_switched(pilot, scr, runner, None, tmp_path)


@pytest.mark.asyncio
async def test_preview_continue_then_stop_and_switch(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot)
        await pilot.press("f2")
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        await pilot.press("enter")  # «продолжить» из превью
        await pilot.pause()
        await _answer(pilot, "enter")
        await _assert_switched(pilot, scr, runner, _B, tmp_path)
