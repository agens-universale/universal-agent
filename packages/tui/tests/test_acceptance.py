"""Acceptance — §7 спеки: основные сценарии не падают."""
import pytest


@pytest.mark.asyncio
async def test_acceptance_full_smoke(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test() as pilot:
        # F1 opens Help
        await pilot.press("f1")
        await pilot.pause()
        from gc_tui.screens.help import HelpScreen
        assert isinstance(pilot.app.screen, HelpScreen)

        # Esc returns
        await pilot.press("escape")
        await pilot.pause()
        from gc_tui.screens.main import TraceScreen
        assert isinstance(pilot.app.screen, TraceScreen)

        # Ctrl+S opens Sessions picker
        await pilot.press("ctrl+s")
        await pilot.pause()
        from gc_tui.screens.sessions import SessionsScreen
        assert isinstance(pilot.app.screen, SessionsScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, TraceScreen)

        # /quit exits the app cleanly
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/quit"
        await pilot.press("enter")
