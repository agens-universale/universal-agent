"""Pilot-тест: сворачивание вставки показывает footer-подсказку."""
from unittest.mock import patch

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


@pytest.mark.asyncio
async def test_paste_hint_lasts_5_seconds():
    """Подсказка повторной вставки держится 5с (а не 2с, как раньше)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_input import CommandTextArea
    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        with patch.object(scr, "set_timer", wraps=scr.set_timer) as spy:
            scr.query_one("#cmdinput", CommandTextArea)._handle_paste("a\n" * 4 + "a")
            await pilot.pause()
        delays = [c.args[0] for c in spy.call_args_list]
        assert 5.0 in delays
