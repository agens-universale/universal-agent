"""Ход по событиям ленты стрелками: Tab выделяет событие, дальше ↑/↓ ходят
по событиям (Shift+Tab занят режимами разрешений), Esc — назад в поле."""

import pytest

pytest.importorskip("textual")

from gc_testkit.tui_fixtures.stub_executor import StubExecutor
from gc_testkit.tui_support import tui_settings


def _app(tmp_path):
    from gc_tui.app import TuiApp

    StubExecutor(
        threads_dir=str(tmp_path), system="cli", scenario="full_dialog"
    ).record(["расскажи"], thread_id="t1")
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )


def _events(screen):
    trace = screen.query_one("#trace")
    return [w for w in trace.children if w.can_focus and w.display]


async def _open(pilot):
    pilot.app.screen.attach_active_task("t1")
    await pilot.pause(0.3)
    return pilot.app.screen


@pytest.mark.asyncio
async def test_arrows_walk_events_once_one_is_selected(tmp_path):
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        screen = await _open(pilot)
        rows = _events(screen)
        assert len(rows) >= 3
        rows[0].focus()
        await pilot.pause()
        await pilot.press("down")
        assert pilot.app.focused is rows[1]
        await pilot.press("down")
        assert pilot.app.focused is rows[2]
        await pilot.press("up")
        assert pilot.app.focused is rows[1]


@pytest.mark.asyncio
async def test_arrows_stop_at_the_ends(tmp_path):
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        screen = await _open(pilot)
        rows = _events(screen)
        rows[0].focus()
        await pilot.pause()
        await pilot.press("up")
        assert pilot.app.focused is rows[0]
        rows[-1].focus()
        await pilot.pause()
        await pilot.press("down")
        assert pilot.app.focused is rows[-1]


@pytest.mark.asyncio
async def test_escape_returns_from_events_to_the_input(tmp_path):
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        screen = await _open(pilot)
        _events(screen)[1].focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.focused is screen.query_one("#cmdinput")
        assert len(pilot.app.screen_stack) == 1  # Esc не ушёл в выход


@pytest.mark.asyncio
async def test_arrows_in_the_input_stay_the_input_s(tmp_path):
    """Фокус в поле — стрелки его (история), события не выделяются."""
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        screen = await _open(pilot)
        inp = screen.query_one("#cmdinput")
        inp.focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert pilot.app.focused is inp
