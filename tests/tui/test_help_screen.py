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

        cmds = "\n".join(str(s.render()) for s in scr.query(".help-row-cmd"))
        descs = "\n".join(str(s.render()) for s in scr.query(".help-row-desc"))
        body = " ".join(str(s.render()) for s in scr.query(Static))

        assert "F1" in descs and "F2" in descs and "F3" in descs
        assert "F4" in descs and "F5" in descs and "F6" in cmds
        assert "F7" not in body.split()

        assert "Ctrl+C" in cmds and "Esc" in descs
        assert "Ctrl+A" in cmds
        assert "Ctrl+N" in descs and "Ctrl+S" in descs
        assert "Ctrl+D" in descs and "Ctrl+O" in descs and "Ctrl+G" in descs
        assert "Ctrl+/" in descs
        assert "Ctrl+E" not in body.split()
        assert str(scr.query_one("#footer", Static).render()) == "[Esc] назад"
        # блок «Связь с разработчиками / Телеграм / Сберчат» убран
        assert "Сберчат" not in body and "Телеграм" not in body
        assert "Связь с разработчиками" not in body
        assert "хоткеи" in body
        assert "как работать" in body
        assert "команды" in body
        # заголовки блоков с разделителями
        assert "── как работать ──" in body
        assert "── команды ──" in body
        # туториал выше списка /команд
        assert body.index("как работать") < body.index("команды")
        assert "/notices" in cmds and "/diff" in cmds and "/compact" in cmds
        # порядок по частоте: help раньше sessions раньше diff
        assert cmds.index("/help") < cmds.index("/sessions") < cmds.index("/diff")
        # якоря мини-туториала (секция «как работать»)
        assert "URL, токен, модель" in descs
        assert "/view <path>" in cmds
        assert "! git status" in cmds
        assert "сразу в буфер" in descs
        assert descs.index("URL, токен, модель") < cmds.index("/help")
        assert "F6" in cmds and "F3" in cmds and "F2" in cmds
        assert "log-txt" in descs and "log-json" in descs
        assert "/export" in cmds
        descs_l = descs.casefold()
        body_l = body.casefold()
        assert "упомянуть файл" in descs_l
        assert "палитра команд" in descs_l
        assert "вложить файл" not in body_l
        assert "Ctrl+J" in cmds
        assert "Ctrl+Enter" not in body
        assert "история ввода" in descs_l
        assert "Shell (!)" in body
        assert "! clear" in cmds
        assert "локальный shell" in descs_l
        assert "буфер обмена" in body
        assert "wl-clipboard" in descs or "xclip" in descs
        assert "Ctrl+Shift+C" in cmds
        assert "не стопает" in descs_l
        assert "." in {c.strip() for c in cmds.splitlines()}


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


@pytest.mark.asyncio
async def test_help_columns_cmd_then_desc():
    """В строке слева команда/клавиша, сразу справа пояснение (не через весь экран)."""
    pytest.importorskip("textual")
    from textual.app import App
    from textual.containers import Horizontal
    from textual.widgets import Static
    from tui.screens.help import HelpScreen

    class _A(App):
        def on_mount(self):
            self.push_screen(HelpScreen())

    async with _A().run_test() as pilot:
        rows = [w for w in pilot.app.screen.query(Horizontal) if w.has_class("help-row")]
        assert rows
        for row in rows[:6]:
            children = list(row.children)
            assert len(children) == 2
            assert children[0].has_class("help-row-cmd")
            assert children[1].has_class("help-row-desc")
        # хоткей slash-команды — в пояснении рядом, не в отдельной правой колонке
        assert len(pilot.app.screen.query(".help-row-key")) == 0
        help_row = next(
            r for r in rows
            if any(
                "/help" in str(c.render())
                for c in r.query(Static)
                if c.has_class("help-row-cmd")
            )
        )
        desc = str(help_row.query_one(".help-row-desc", Static).render())
        assert "Справка" in desc and "F1" in desc
