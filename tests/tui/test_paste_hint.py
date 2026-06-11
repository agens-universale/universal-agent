"""Pilot-тест: сворачивание вставки показывает footer-подсказку."""
import pytest


@pytest.mark.asyncio
async def test_paste_collapse_shows_footer_hint():
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tui.app import TuiApp
    from tui.widgets.command_input import CommandTextArea
    async with TuiApp().run_test() as pilot:
        inp = pilot.app.screen.query_one("#cmdinput", CommandTextArea)
        inp._handle_paste("a\n" * 4 + "a")          # 5 строк → сворачивается
        await pilot.pause()
        left = pilot.app.screen.query_one("#footer-left", Static)
        assert "развернуть" in str(left.render())
