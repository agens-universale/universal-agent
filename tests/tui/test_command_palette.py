"""Палитра команд: фильтрация, ↑↓, Tab/Enter/Esc."""
import pytest


def test_palette_shows_all_commands_no_limit():
    pytest.importorskip("textual")
    from tui.commands import build_default_registry
    from tui.widgets.command_palette import CommandPalette

    reg = build_default_registry()
    palette = CommandPalette(reg, initial_filter="")
    rows = palette._visible_rows()
    # All registry commands present — no truncation.
    assert len(rows) == len(reg.list_commands())
    assert len(rows) >= 9


@pytest.mark.asyncio
async def test_palette_opens_on_slash_and_filters():
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        await pilot.press("/")
        await pilot.press("s")
        palette = pilot.app.query_one(".palette")
        rows_text = "\n".join(
            str(r.render()) for r in palette.query(".palette-row")
        )
        assert "/skills" in rows_text
        assert "/stop" in rows_text
        assert "/quit" not in rows_text  # filtered out


@pytest.mark.asyncio
async def test_palette_opens_on_dot_only_and_closes_on_more_typing(tmp_path):
    """Ровно `.` открывает палитру (RU-раскладка); дальше печатаем — закрывается."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_palette import CommandPalette

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "."
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 1
        palette = pilot.app.query_one(CommandPalette)
        rows_text = "\n".join(
            str(r.render()) for r in palette.query(".palette-row")
        )
        assert "/help" in rows_text
        assert "/sessions" in rows_text

        # Продолжили печатать — палитра пропала (не фильтр по `.x`).
        inp.text = ".п"
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 0


@pytest.mark.asyncio
async def test_palette_dot_enter_executes_selected(tmp_path):
    """Enter по палитре, открытой через `.`, выполняет /команду — не шлёт `.`."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.help import HelpScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "."
        await pilot.pause(0.1)
        assert pilot.app.query(".palette")
        # Первая в реестре — help.
        await pilot.press("enter")
        assert not pilot.app.query(".palette")
        assert pilot.app.query_one("#cmdinput").text == ""
        assert isinstance(pilot.app.screen, HelpScreen)


@pytest.mark.asyncio
async def test_palette_dot_tab_completes_with_slash(tmp_path):
    """Tab из палитры по `.` подставляет /name — не `.name`."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "."
        await pilot.pause(0.1)
        await pilot.press("tab")
        assert not pilot.app.query(".palette")
        assert pilot.app.query_one("#cmdinput").text == "/help "


@pytest.mark.asyncio
async def test_palette_closes_on_esc():
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        await pilot.press("/")
        assert pilot.app.query(".palette")
        await pilot.press("escape")
        assert not pilot.app.query(".palette")


@pytest.mark.asyncio
async def test_palette_enter_inserts_command_with_args(tmp_path):
    """Enter по команде с аргументами (/view) подставляет в input, не выполняет."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.file_view import FileViewerScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        await pilot.press("/")
        await pilot.press("v")  # view
        await pilot.press("enter")
        assert not pilot.app.query(".palette")
        inp = pilot.app.query_one("#cmdinput")
        assert inp.text == "/view "
        # Курсор в конце — дальше можно дописывать путь.
        assert inp.cursor_location == inp.document.end
        # Экран viewer ещё не открыт.
        assert not isinstance(pilot.app.screen, FileViewerScreen)


@pytest.mark.asyncio
async def test_palette_enter_inserts_export_then_field_enter_runs(tmp_path, monkeypatch):
    """Из палитры Enter только подставляет `/export `; выполнение — следующим Enter в поле."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from thread_store import append_new_thread_messages
    from tui.app import TuiApp
    from tui.services.stores.file_store import FileTaskStore
    from tui.widgets.arg_picker import ArgPicker
    from tui.widgets.command_palette import CommandPalette

    append_new_thread_messages(
        str(tmp_path), "cli", "tid1",
        [{"role": "user", "content": "вопрос", "id": "m1"}], set(),
    )
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "tid1"
        scr._store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
        await pilot.press("/")
        await pilot.press("e")
        await pilot.press("x")
        await pilot.press("enter")
        await pilot.pause()
        # Палитра команд закрыта; в поле `/export ` + arg-picker форматов.
        assert not pilot.app.query(CommandPalette)
        inp = pilot.app.query_one("#cmdinput")
        assert inp.text == "/export "
        assert len(pilot.app.query(ArgPicker)) == 1
        assert list(tmp_path.glob("export-tid1-*.txt")) == []

        await pilot.press("enter")
        await pilot.pause()
        files = list(tmp_path.glob("export-tid1-*.txt"))
        assert len(files) == 1
        assert "вопрос" in files[0].read_text(encoding="utf-8")
        assert inp.text == ""


@pytest.mark.asyncio
async def test_palette_enter_executes_selected():
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        await pilot.press("/")
        await pilot.press("s")          # sessions, settings, skills, skill, status, stop
        await pilot.press("down")
        await pilot.press("down")       # index 2: skills
        await pilot.press("enter")
        # Palette closed
        assert not pilot.app.query(".palette")
        # Input cleared
        assert pilot.app.query_one("#cmdinput").text == ""
        # The selected command (skills) ran — pushed SkillsScreen
        from tui.screens.skills import SkillsScreen
        assert isinstance(pilot.app.screen, SkillsScreen)
        # Typed prefix did NOT run as a literal command
        assert not pilot.app.query(".palette")


@pytest.mark.asyncio
async def test_palette_closes_after_space_after_command_name(tmp_path):
    """После '/export ' (с пробелом) палитра не должна быть видна."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_palette import CommandPalette

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/ex"
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 1

        inp.text = "/export"
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 1

        # Пробел после имени → палитра исчезает.
        inp.text = "/export "
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 0

        # Снова без пробела → палитра возвращается.
        inp.text = "/export"
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 1


@pytest.mark.asyncio
async def test_palette_first_row_selected_on_open(tmp_path):
    """При открытии палитры подсвечена первая строка (а не «никакая»)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_palette import CommandPalette

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.query_one("#cmdinput").text = "/"
        await pilot.pause(0.1)
        palette = pilot.app.query_one(CommandPalette)
        rows = palette.query(".palette-row")
        assert len(rows) >= 2
        assert rows[0].has_class("selected")
        assert not rows[1].has_class("selected")


def test_registry_does_not_contain_main():
    pytest.importorskip("textual")
    from tui.commands import build_default_registry
    reg = build_default_registry()
    assert reg.lookup("main") is None


@pytest.mark.asyncio
async def test_palette_hugs_content(tmp_path):
    """Рамка палитры обнимает список (height: auto) — не растянута на весь экран."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_palette import CommandPalette
    from tui.commands import build_default_registry

    app = TuiApp(threads_dir=str(tmp_path / "t"), config={"model": "M"})
    async with app.run_test(size=(80, 40)) as pilot:
        reg = build_default_registry()
        pal = CommandPalette(reg, initial_filter="se")   # фильтр → 2 реальные команды (/sessions, /settings)
        await app.screen.mount(pal)
        await pilot.pause()
        rows = pal._visible_rows()
        assert len(rows) >= 1  # непустой список — не вырожденный нулевой случай
        assert pal.styles.height is not None and pal.styles.height.is_auto  # height: auto
        assert pal.region.height < 40                                      # не на весь экран
        assert pal.region.height < 10                                      # рамка обнимает малый список, не растянута


@pytest.mark.asyncio
async def test_palette_keys_hint_has_air_above(tmp_path):
    """Строка клавиш отделена от списка команд воздухом (то же правило, что `.set-hint`
    в настройках) — иначе читается как ещё один пункт списка. «Нет совпадений» воздуха
    не получает: там отделять нечего."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_palette import CommandPalette
    from tui.commands import build_default_registry

    app = TuiApp(threads_dir=str(tmp_path / "t"), config={"model": "M"})
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(build_default_registry(), initial_filter="se")
        await app.screen.mount(pal)
        await pilot.pause()
        keys = pal.query_one(".palette-keys")
        assert keys.styles.margin.top == 1
        rows = list(pal.query(".palette-row"))
        assert rows and rows[-1].styles.margin.top == 0     # у команд воздуха нет
