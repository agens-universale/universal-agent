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
        # хоткеи slash-команд живут своей колонкой — ищем их там, а не в пояснении
        keys = "\n".join(str(s.render()) for s in scr.query(".help-row-key"))
        body = " ".join(str(s.render()) for s in scr.query(Static))

        assert "F1" in keys and "F2" in keys and "F3" in keys
        assert "F4" in keys and "F5" in keys and "F6" in keys and "F6" in cmds
        # B10: F7 и секция «буфер сессии» явно описаны в /help. Ищем по всему экрану:
        # F7 стоит и колонкой хоткея у /buffer, и подписью в блоке «буфер сессии».
        assert "F7" in body

        assert "Ctrl+C" in cmds and "Esc" in descs
        assert "Ctrl+A" in cmds
        assert "Ctrl+N" in keys and "Ctrl+S" in keys
        assert "Ctrl+T" in keys and "Ctrl+T" in cmds   # голос: и у команды, и в хоткеях
        assert "Ctrl+D" in keys and "Ctrl+O" in keys and "Ctrl+G" in keys
        assert "Ctrl+/" in keys
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
        assert "плашка" in descs.casefold()
        assert "целиком" in descs.casefold()
        assert "Ctrl+V" in cmds
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
        # B10: секция про буфер сессии — три буфера, Esc=сохранить, Ctrl+C дописывает
        assert "── буфер сессии ──" in body
        assert "три буфера" in body_l
        assert "сохранить и закрыть" in descs_l
        assert "дописывает" in descs_l or "копирует" in descs_l


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
            assert len(children) == 3
            assert children[0].has_class("help-row-cmd")
            assert children[1].has_class("help-row-desc")
            assert children[2].has_class("help-row-key")
        # хоткей slash-команды — своей колонкой справа: в хвосте описаний разной
        # длины он вставал лесенкой, и глазами его было не собрать
        help_row = next(
            r for r in rows
            if any(
                "/help" in str(c.render())
                for c in r.query(Static)
                if c.has_class("help-row-cmd")
            )
        )
        desc = str(help_row.query_one(".help-row-desc", Static).render())
        key = str(help_row.query_one(".help-row-key", Static).render())
        assert "Справка" in desc and "F1" not in desc
        assert key == "F1 · Ctrl+/"


@pytest.mark.asyncio
async def test_hotkey_column_does_not_drift_with_the_window_width():
    """Колонка хоткеев привязана к описаниям, а не к правому краю: её позиция одна и
    та же на узком и на широком терминале. При 1fr у описания она уезжала вслед за
    краем, разрывая строку полем пустоты."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.help import HelpScreen

    async def _hotkey_col(width: int) -> int:
        app = TuiApp(config={"model": "M"})
        async with app.run_test(size=(width, 60)) as pilot:
            await app.push_screen(HelpScreen())
            await pilot.pause()
            rows = ["".join(seg.text for seg in strip)
                    for strip in app.screen._compositor.render_strips()]
            # именно строки команд: имя стоит в ПЕРВОЙ колонке. Проверять начало
            # строки нельзя — на узком окне описания переносятся, и продолжение
            # «/settings (MCP…)» из блока «как работать» тоже начинается с «/».
            def _row(name: str) -> str:
                return next(r for r in rows if r[:20].strip() == name)

            line, voice = _row("/settings"), _row("/voice")
            assert voice.index("Ctrl+T") == line.index("F6")   # колонка общая
            return line.index("F6")

    assert await _hotkey_col(120) == await _hotkey_col(170)


@pytest.mark.asyncio
async def test_long_description_without_hotkey_does_not_break_in_two():
    """Колонка описаний меряется по самому длинному описанию ЛЮБОЙ строки, а не только
    той, у которой есть клавиша. Раньше считали по строкам с хоткеем в расчёте, что
    длинные строки без клавиши «заедут в пустую колонку»; колонка фиксированной ширины,
    и они ломались пополам — /rigor, /gate, /answers занимали по две строки (поймано
    живой приёмкой)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.commands import build_default_registry
    from tui.screens.help import HelpScreen

    names = {"/" + c.name for c in build_default_registry().list_commands()}
    app = TuiApp(config={"model": "M"})
    async with app.run_test(size=(130, 60)) as pilot:   # ширины хватает, потолок не при чём
        await app.push_screen(HelpScreen())
        await pilot.pause()
        rows = [r for r in app.screen.query(".help-row")
                if str(list(r.children)[0].render()).strip() in names]
        assert rows, "строк команд не нашлось"
        tall = [str(list(r.children)[0].render()).strip()
                for r in rows if r.size.height > 1]
        assert tall == [], f"описания ломаются пополам: {tall}"


def test_zebra_stripe_is_lighter_than_the_background():
    """Полоса зебры должна отличаться от фона заметно — на прежнем #181c26 разницу
    было не поймать глазом."""
    from pathlib import Path
    css = Path("src/tui/styles.tcss").read_text(encoding="utf-8")
    line = next(l for l in css.splitlines() if ".help-row-alt" in l)
    stripe = line.split("background:")[1].strip().rstrip(";} ")
    bg = "#0E1119"
    to_rgb = lambda h: tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))
    delta = sum(abs(a - b) for a, b in zip(to_rgb(stripe), to_rgb(bg)))
    assert delta >= 45, f"{stripe} слишком близок к фону ({delta})"


@pytest.mark.asyncio
async def test_quickstart_mentions_voice_input():
    """Блок «как работать» — сценарий, а не справочник (F6, F2, F3, /new, Esc там уже
    повторяются): голос в нём нужен рядом с Enter, как альтернатива набору."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.help import HelpScreen

    app = TuiApp(config={"model": "M"})
    async with app.run_test(size=(170, 60)) as pilot:
        await app.push_screen(HelpScreen())
        await pilot.pause()
        rows = ["".join(seg.text for seg in strip).rstrip()
                for strip in app.screen._compositor.render_strips()]
        top = next(i for i, r in enumerate(rows) if "как работать" in r)
        block = rows[top:top + 6]
        voice = next(r for r in block if r[:20].strip() == "Ctrl+T")
        assert "надиктуйте сообщение вместо набора" in voice
        assert "/settings" in voice                     # без сервиса клавиша молчит
        enter = next(i for i, r in enumerate(block) if r[:20].strip() == "Enter")
        assert block.index(voice) == enter + 1          # сразу за строкой про набор
