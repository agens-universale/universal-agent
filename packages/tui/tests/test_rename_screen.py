"""RenameScreen: префилл, Enter → строка, Esc → None."""
import pytest


@pytest.mark.asyncio
async def test_enter_returns_trimmed_value(tmp_path):
    pytest.importorskip("textual")
    from textual.app import App
    from textual.widgets import Input
    from gc_tui.screens.rename import RenameScreen

    result = {}

    class _A(App):
        def on_mount(self):
            self.push_screen(RenameScreen(current="старый"), lambda v: result.update(v=v))

    async with _A().run_test() as pilot:
        inp = pilot.app.screen.query_one("#rename-input", Input)
        assert inp.value == "старый"
        inp.value = "  новый  "
        await pilot.press("enter")
        await pilot.pause()
    assert result["v"] == "новый"


@pytest.mark.asyncio
async def test_escape_returns_none(tmp_path):
    pytest.importorskip("textual")
    from textual.app import App
    from gc_tui.screens.rename import RenameScreen

    result = {}

    class _A(App):
        def on_mount(self):
            self.push_screen(RenameScreen(current="x"), lambda v: result.update(v=v))

    async with _A().run_test() as pilot:
        await pilot.press("escape")
        await pilot.pause()
    assert result["v"] is None
