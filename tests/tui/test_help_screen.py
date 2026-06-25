import pytest


@pytest.mark.asyncio
async def test_help_header_and_hotkeys():
    pytest.importorskip("textual")
    from textual.app import App
    from textual.widgets import Static
    from tui.screens.help import HelpScreen

    class _A(App):
        def on_mount(self):
            self.push_screen(HelpScreen())

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        assert "Справка" in str(scr.query_one("#header-left", Static).render())
        # правый бренд-блок убран со всех подэкранов
        assert len(scr.query("#header-right")) == 0
        keys = " ".join(str(s.render()) for s in scr.query(".help-row-key"))
        assert "Ctrl+S" in keys and "F1" in keys and "Ctrl+N" in keys
        assert "Ctrl+E" not in keys
        assert str(scr.query_one("#footer", Static).render()) == "[Esc] назад"
        # блок «Связь с разработчиками / Телеграм / Сберчат» убран
        body = " ".join(str(s.render()) for s in scr.query(Static))
        assert "Сберчат" not in body and "Телеграм" not in body
        assert "Связь с разработчиками" not in body


@pytest.mark.asyncio
async def test_help_rows_zebra_striping():
    """Строки help чередуют фон: нечётные — с классом help-row-alt, чётные — без."""
    pytest.importorskip("textual")
    from textual.app import App
    from tui.screens.help import HelpScreen
    from textual.containers import Horizontal

    class _A(App):
        def on_mount(self):
            self.push_screen(HelpScreen())

    async with _A().run_test() as pilot:
        rows = [w for w in pilot.app.screen.query(Horizontal) if w.has_class("help-row")]
        assert len(rows) >= 4
        assert not rows[0].has_class("help-row-alt")
        assert rows[1].has_class("help-row-alt")
        assert not rows[2].has_class("help-row-alt")


@pytest.mark.asyncio
async def test_help_trace_not_focusable():
    """Список help не фокусируется → нет зелёной focus-рамки (#trace:focus outline)."""
    pytest.importorskip("textual")
    from textual.app import App
    from textual.containers import VerticalScroll
    from tui.screens.help import HelpScreen

    class _A(App):
        def on_mount(self):
            self.push_screen(HelpScreen())

    async with _A().run_test() as pilot:
        trace = pilot.app.screen.query_one("#trace", VerticalScroll)
        assert trace.can_focus is False
        assert pilot.app.focused is not trace
