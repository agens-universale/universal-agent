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
        assert "справка" in str(scr.query_one("#header-left", Static).render())
        # правый бренд-блок убран со всех подэкранов
        assert len(scr.query("#header-right")) == 0
        keys = " ".join(str(s.render()) for s in scr.query(".help-row-key"))
        assert "F1" in keys and "F2" in keys and "F3" in keys
        assert "F4" in keys and "F5" in keys
        key_tokens = set(keys.split())
        assert "F6" not in key_tokens and "F7" not in key_tokens
        assert "Ctrl+C" in keys and "Esc" in keys
        assert "Ctrl+A" in keys
        assert "Ctrl+N" in keys and "Ctrl+S" in keys
        assert "Ctrl+D" in keys and "Ctrl+O" in keys and "Ctrl+G" in keys
        assert "Ctrl+/" in keys
        assert "Ctrl+E" not in key_tokens
        assert str(scr.query_one("#footer", Static).render()) == "[Esc] назад"
        # блок «Связь с разработчиками / Телеграм / Сберчат» убран
        body = " ".join(str(s.render()) for s in scr.query(Static))
        assert "Сберчат" not in body and "Телеграм" not in body
        assert "Связь с разработчиками" not in body
        assert "хоткеи" in body
        mains = "\n".join(str(s.render()) for s in scr.query(".help-row-main"))
        assert "/notices" in mains and "/diff" in mains and "/compact" in mains
        # порядок по частоте: help раньше sessions раньше diff
        assert mains.index("/help") < mains.index("/sessions") < mains.index("/diff")
        assert "log-txt" in mains and "log-json" in mains
        assert "упомянуть файл" in mains
        assert "палитра команд" in mains
        assert "вложить файл" not in mains
        assert "Ctrl+J" in keys
        assert "Ctrl+Enter" not in keys
        assert "история ввода" in mains
        assert "Shell (!)" in body
        assert "! git status" in mains
        assert "! clear" in mains
        assert "локальный shell" in mains
        keys_all = " ".join(str(s.render()) for s in scr.query(".help-row-key"))
        assert "." in keys_all.split()


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
