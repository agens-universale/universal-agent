"""Палитра команд: фильтрация, ↑↓, Tab/Enter/Esc."""

import pytest
from gc_testkit.tui_support import tui_settings, wait_until


def test_palette_shows_all_commands_no_limit():
    pytest.importorskip("textual")
    from gc_tui.commands import build_default_registry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = build_default_registry()
    palette = CommandPalette(reg, initial_filter="")
    rows = palette._visible_rows()
    # Все обычные (не-toggle) команды реестра — без усечения. Toggle-команды
    # (/gate, /answers) живут в отдельном подвале, не в этой (скроллируемой) зоне.
    non_toggle = [c for c in reg.list_commands() if not c.is_toggle]
    assert len(rows) == len(non_toggle)
    assert len(rows) >= 9


@pytest.mark.asyncio
async def test_palette_opens_on_slash_and_filters():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

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
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_palette import CommandPalette

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
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
    from gc_tui.app import TuiApp
    from gc_tui.screens.help import HelpScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
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
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
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
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        await pilot.press("/")
        assert pilot.app.query(".palette")
        await pilot.press("escape")
        assert not pilot.app.query(".palette")


@pytest.mark.asyncio
async def test_palette_enter_inserts_command_with_args(tmp_path):
    """Enter по команде с аргументами (/view) подставляет в input, не выполняет.
    /view — команда с arg_file: следом за подстановкой открывается file-picker
    (состояние 3b), как /export открывает arg-picker — см. соседний тест."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen
    from gc_tui.widgets.command_palette import CommandPalette
    from gc_tui.widgets.file_picker import FilePicker

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        await pilot.press("/")
        await pilot.press("v")  # view
        await pilot.press("enter")
        assert not pilot.app.query(CommandPalette)
        assert len(pilot.app.query(FilePicker)) == 1
        inp = pilot.app.query_one("#cmdinput")
        assert inp.text == "/view "
        # Курсор в конце — дальше можно дописывать путь.
        assert inp.cursor_location == inp.document.end
        # Экран viewer ещё не открыт.
        assert not isinstance(pilot.app.screen, FileViewerScreen)


@pytest.mark.asyncio
async def test_palette_enter_inserts_export_then_field_enter_runs(
    tmp_path, monkeypatch
):
    """Из палитры Enter только подставляет `/export `; выполнение — следующим Enter в поле."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_testkit.develop_files import append_new_thread_messages
    from gc_tui.app import TuiApp
    from gc_tui.services.stores.file_store import FileTaskStore
    from gc_tui.widgets.arg_picker import ArgPicker
    from gc_tui.widgets.command_palette import CommandPalette

    append_new_thread_messages(
        str(tmp_path),
        "cli",
        "tid1",
        [{"role": "user", "content": "вопрос", "id": "m1"}],
        set(),
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
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
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        await pilot.press("/")
        await pilot.press(
            "s"
        )  # sessions, settings, skills, skill, status, stop
        await pilot.press("down")
        await pilot.press("down")  # index 2: skills
        await pilot.press("enter")
        # Palette closed
        assert not pilot.app.query(".palette")
        # Input cleared
        assert pilot.app.query_one("#cmdinput").text == ""
        # The selected command (skills) ran — pushed SkillsScreen
        from gc_tui.screens.skills import SkillsScreen

        assert isinstance(pilot.app.screen, SkillsScreen)
        # Typed prefix did NOT run as a literal command
        assert not pilot.app.query(".palette")


@pytest.mark.asyncio
async def test_palette_closes_after_space_after_command_name(tmp_path):
    """После '/export ' (с пробелом) палитра не должна быть видна."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_palette import CommandPalette

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )

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
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_palette import CommandPalette

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        pilot.app.query_one("#cmdinput").text = "/"
        await pilot.pause(0.1)
        palette = pilot.app.query_one(CommandPalette)
        rows = palette.query(".palette-row")
        assert len(rows) >= 2
        assert rows[0].has_class("selected")
        assert not rows[1].has_class("selected")


def test_descriptions_align_to_common_name_column():
    """Спека 2026-07-18 §4: описания всех строк начинаются в общей колонке —
    имя паддируется до самого длинного имени среди видимых строк + 3 пробела."""
    pytest.importorskip("textual")
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("s", (), "", "короткое"))
    reg.register(Command("verylongname", (), "", "длинное"))
    palette = CommandPalette(reg, initial_filter="")

    short_row = palette._format_row(reg.lookup("s"))
    long_row = palette._format_row(reg.lookup("verylongname"))

    assert short_row.index("короткое") == long_row.index("длинное")
    # Ровно 3 пробела после самого длинного имени (спека §4).
    assert long_row == " /verylongname   длинное"


def test_hotkeys_align_to_a_common_column():
    """Хоткеи строк — ровной колонкой, как и описания: в хвосте описаний разной длины
    они вставали лесенкой, и клавишу приходилось выискивать глазами."""
    pytest.importorskip("textual")
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("a", (), "", "короткое", hotkey="F1"))
    reg.register(Command("b", (), "", "описание подлиннее", hotkey="Ctrl+T"))
    reg.register(Command("c", (), "", "без клавиши"))
    palette = CommandPalette(reg, initial_filter="")

    rows = {n: palette._format_row(reg.lookup(n)) for n in ("a", "b", "c")}
    assert len(rows["a"]) == len(rows["b"])  # правые края совпадают
    assert rows["c"] == rows["c"].rstrip()  # без хоткея — без пустого хвоста
    # Вне приложения ширина — естественная: зазор 3 после длинного описания.
    assert rows["b"].endswith("описание подлиннее   Ctrl+T")
    assert rows["a"].endswith("F1".rjust(len("Ctrl+T")))


def test_hotkey_column_counts_rows_without_hotkeys_too():
    """Колонка считается по самому длинному описанию ЛЮБОЙ строки, а не только той,
    у которой есть клавиша: иначе у длинного описания без хоткея зазора не остаётся."""
    pytest.importorskip("textual")
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("a", (), "", "короткое", hotkey="F1"))
    reg.register(
        Command("b", (), "", "описание намного длиннее и БЕЗ клавиши")
    )
    palette = CommandPalette(reg, initial_filter="")

    row_a = palette._format_row(reg.lookup("a"))
    row_b = palette._format_row(reg.lookup("b"))
    assert row_a.index("F1") == len(row_b) + 3  # правее длинной, зазор 3


def test_palette_has_no_hotkey_column_when_nobody_has_one():
    """Колонку заводим по факту: если у видимых строк клавиш нет, лишних пробелов тоже
    нет (палитра фильтруется, и на одной строке колонка была бы пустой тратой места)."""
    pytest.importorskip("textual")
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("a", (), "", "первое"))
    reg.register(Command("b", (), "", "второе"))
    palette = CommandPalette(reg, initial_filter="")
    assert palette._hotkey_col_w() == 0
    assert palette._format_row(reg.lookup("a")) == " /a   первое"


def test_registry_does_not_contain_main():
    pytest.importorskip("textual")
    from gc_tui.commands import build_default_registry

    reg = build_default_registry()
    assert reg.lookup("main") is None


@pytest.mark.asyncio
async def test_palette_hugs_content(tmp_path):
    """Рамка палитры обнимает список (height: auto) — не растянута на весь экран."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.commands import build_default_registry
    from gc_tui.widgets.command_palette import CommandPalette

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 40)) as pilot:
        reg = build_default_registry()
        pal = CommandPalette(
            reg, initial_filter="se"
        )  # фильтр → 2 реальные команды (/sessions, /settings)
        await app.screen.mount(pal)
        await pilot.pause()
        rows = pal._visible_rows()
        assert (
            len(rows) >= 1
        )  # непустой список — не вырожденный нулевой случай
        assert (
            pal.styles.height is not None and pal.styles.height.is_auto
        )  # height: auto
        assert pal.region.height < 40  # не на весь экран
        assert (
            pal.region.height < 10
        )  # рамка обнимает малый список, не растянута


@pytest.mark.asyncio
async def test_palette_keys_hint_has_air_above(tmp_path):
    """Строка клавиш отделена от списка команд воздухом (то же правило, что `.set-hint`
    в настройках) — иначе читается как ещё один пункт списка. «Нет совпадений» воздуха
    не получает: там отделять нечего."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.commands import build_default_registry
    from gc_tui.widgets.command_palette import CommandPalette

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(build_default_registry(), initial_filter="se")
        await app.screen.mount(pal)
        await pilot.pause()
        keys = pal.query_one(".palette-keys")
        assert keys.styles.margin.top == 1
        rows = list(pal.query(".palette-row"))
        assert rows and rows[-1].styles.margin.top == 0  # у команд воздуха нет


@pytest.mark.asyncio
async def test_long_description_row_stays_one_line(tmp_path):
    """Геометрия палитры держится на «одна команда — одна строка»: `desired_height`
    (строки + подвал + рамка) становится min-height палитры, и трейс
    получает ровно остаток экрана. Описание, не влезающее в ширину терминала,
    переносилось на вторую строку — фактическая высота палитры оказывалась больше
    заявленной, и нижняя рамка/футер уезжали за край экрана (тем сильнее, чем
    длиннее описания и чем больше команд). Лекарство — канон `.onb-mcp-row` /
    `.set-rowtext`: строка не переносится, хвост срезается многоточием."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("short", (), "", "Короткое описание"))
    reg.register(
        Command(
            "long",
            (),
            "",
            "Очень длинное описание команды, которое "
            "заведомо не влезает в ширину терминала и "
            "раньше переносилось на вторую строку",
        )
    )

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(reg, initial_filter="")
        await app.screen.mount(pal)
        await pilot.pause()
        for row in pal.query(".palette-row"):
            assert row.region.height == 1, (
                "строка палитры не должна переноситься"
            )
        assert pal.region.height == pal.desired_height


def test_enter_runs_a_command_whose_argument_is_optional_without_a_menu():
    """Общее правило: аргумента нет либо он необязателен и меню вариантов у
    него нет — Enter выполняет; иначе подставляет команду в поле."""
    pytest.importorskip("textual")
    from gc_tui.commands import build_default_registry
    from gc_tui.widgets.command_palette import (
        _HINT_NO_ARGS,
        _HINT_WITH_ARGS,
        _hint_for,
    )

    reg = build_default_registry()
    for name in ("plan", "new", "help"):
        command = reg.lookup(name)
        assert command.runs_on_enter is True
        assert _hint_for(command) == _HINT_NO_ARGS
    for name in ("export", "view", "skill"):
        command = reg.lookup(name)
        assert command.runs_on_enter is False
        assert _hint_for(command) == _HINT_WITH_ARGS


# --- Строки скиллов (спека P5 §3.1) ---------------------------------------


def _skill_palette(filter_, skills, commands=("branch", "status")):
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    for name in commands:
        reg.register(Command(name, (), "", f"команда {name}"))
    return CommandPalette(
        reg, initial_filter=filter_, skills=lambda: list(skills)
    )


_BRAIN = {
    "id": "superpowers:brainstorming",
    "pack_id": "superpowers",
    "description": "мозговой штурм",
}


def test_bare_slash_shows_no_skills():
    pytest.importorskip("textual")
    palette = _skill_palette("", [_BRAIN])
    assert palette._visible_skill_rows() == []
    assert palette._skill_more_count() == 0


def test_skill_matches_short_name_prefix():
    pytest.importorskip("textual")
    palette = _skill_palette("brai", [_BRAIN, {"id": "other:x"}])
    assert palette._visible_skill_rows() == [_BRAIN]


_SUPER = [
    {"id": "supercoder:brainstorming"},
    {"id": "supercoder:coder"},
    {"id": "superpowers:debugging"},
    {"id": "other:systematic-debugging"},
    {"id": "solo"},
]


def _shown(palette):
    return [
        (r.get("input_name") or r["id"].rpartition(":")[2], r["id"])
        for r in palette._visible_skill_rows()
    ]


def test_skill_matches_pack_prefix_shown_in_full_below_name_matches():
    pytest.importorskip("textual")
    palette = _skill_palette("s", _SUPER)
    assert _shown(palette) == [
        ("solo", "solo"),
        ("systematic-debugging", "other:systematic-debugging"),
        ("supercoder:brainstorming", "supercoder:brainstorming"),
        ("supercoder:coder", "supercoder:coder"),
        ("superpowers:debugging", "superpowers:debugging"),
    ]


@pytest.mark.parametrize("typed", ["sup", "supercoder"])
def test_pack_prefix_lists_pack_skills_in_full(typed):
    pytest.importorskip("textual")
    palette = _skill_palette(typed, _SUPER)
    names = [n for n, _ in _shown(palette)]
    assert names[:2] == ["supercoder:brainstorming", "supercoder:coder"]
    assert "systematic-debugging" not in names


def test_name_and_pack_match_is_listed_once_short():
    pytest.importorskip("textual")
    skills = [{"id": "debug:debugging"}]
    assert _shown(_skill_palette("deb", skills)) == [
        ("debugging", "debug:debugging")
    ]


def test_name_match_stays_short_for_cyrillic_prefix():
    pytest.importorskip("textual")
    skills = [{"id": "ru:бренд"}, {"id": "бр:другой"}]
    assert _shown(_skill_palette("бр", skills)) == [
        ("бренд", "ru:бренд"),
        ("бр:другой", "бр:другой"),
    ]


def test_pack_without_prefix_match_and_flat_skill_match_by_name_only():
    pytest.importorskip("textual")
    palette = _skill_palette("so", _SUPER)
    assert _shown(palette) == [("solo", "solo")]


def test_enter_inserts_shown_name():
    pytest.importorskip("textual")
    palette = _skill_palette("supercoder", _SUPER)
    assert palette.selected_skill()["input_name"] == (
        "supercoder:brainstorming"
    )


def test_skill_full_id_prefix_only_with_colon():
    pytest.importorskip("textual")
    skills = [
        {"id": "default-coworker:background-business"},
        {"id": "default-coworker:handoff-autonomy"},
        {"id": "x:deliverables-evidence"},
    ]
    palette = _skill_palette("default-coworker:", skills)
    assert palette._visible_skill_rows() == skills[:2]


def _budget_palette(filter_, n_skills, cap, commands=("zzz",)):
    skills = [{"id": f"p:brai-{i}"} for i in range(n_skills)]
    palette = _skill_palette(filter_, skills, commands=commands)
    palette.styles.max_height = cap
    return palette, skills


def test_skills_use_rows_left_by_commands():
    pytest.importorskip("textual")
    # cap 20 - рамка 2 - подвал 2 - 1 команда = 15 свободных строк
    palette, skills = _budget_palette("brai", 9, 20, commands=("brai",))
    assert palette._visible_skill_rows() == skills
    assert palette._skill_more_count() == 0


def test_single_hidden_skill_is_shown_instead_of_more_line():
    pytest.importorskip("textual")
    palette, skills = _budget_palette("brai", 6, 0)
    assert palette._visible_skill_rows() == skills
    assert palette._skill_more_count() == 0


def test_floor_five_skills_when_commands_fill_budget():
    pytest.importorskip("textual")
    cmds = tuple(f"braic{i}" for i in range(30))
    palette, skills = _budget_palette("brai", 8, 12, commands=cmds)
    assert palette._visible_skill_rows() == skills[:5]
    assert palette._skill_more_count() == 3


def test_skill_match_lowercases_filter_like_commands():
    pytest.importorskip("textual")
    palette = _skill_palette("BRAI", [_BRAIN])
    assert palette._visible_skill_rows() == [_BRAIN]


def test_skill_rows_capped_at_five_with_more_line():
    pytest.importorskip("textual")
    skills = [{"id": f"p:brai-{i}"} for i in range(7)]
    palette = _skill_palette("brai", skills)
    palette.styles.max_height = 0
    assert palette._visible_skill_rows() == skills[:5]
    assert palette._skill_more_count() == 2
    assert palette._skill_more_text() == "… ещё 2 — /skill brai"


def test_skill_shadowed_by_command_or_alias_is_hidden():
    pytest.importorskip("textual")
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("status", (), "", "статус"))
    reg.register(Command("model", ("models",), "", "модель"))
    skills = [
        {"id": "status"},
        {"id": "pack:status"},
        {"id": "pack:models"},
        {"id": "pack:statusline"},
    ]
    palette = CommandPalette(reg, initial_filter="s", skills=lambda: skills)
    assert palette._visible_skill_rows() == [{"id": "pack:statusline"}]
    palette.set_filter("m")
    assert palette._visible_skill_rows() == []


def test_long_skill_id_widens_name_column_only_up_to_cap():
    pytest.importorskip("textual")
    long_skill = {"id": "pack:b" + "x" * 60, "description": "d" * 80}
    plain = _skill_palette("b", [])
    with_skill = _skill_palette("b", [long_skill])
    assert with_skill._hotkey_col_w() == plain._hotkey_col_w()
    assert with_skill._name_col_w() == 32 + 3  # потолок общей колонки


def test_skill_row_text_has_name_and_description_without_pack():
    pytest.importorskip("textual")
    from gc_tui import palette as _pal

    skill = dict(_BRAIN, id="superpowers:bzz")
    palette = _skill_palette("b", [skill])
    head = f" {'/bzz'.ljust(len('/bzz') + 3)}"
    text = palette._format_skill_row(skill)
    # колонка общая: самое длинное имя — команда `/status`
    head = f" {'/bzz'.ljust(len('/status') + 3)}"
    assert text.plain == head + "мозговой штурм"
    assert "superpowers" not in text.plain
    assert "·" not in text.plain
    assert {str(s.style) for s in text.spans} == {_pal.DIM}
    assert text.spans[0].start == len(head)


def test_skill_row_shows_runnable_name_from_provider():
    pytest.importorskip("textual")
    skill = {"id": "a:dup", "input_name": "a:dup", "description": "d"}
    palette = _skill_palette("d", [skill])
    text = palette._format_skill_row(skill)
    assert text.plain == " /a:dup   d"


def test_skill_rows_show_full_names_in_shared_column():
    pytest.importorskip("textual")
    skills = [
        {"id": "p:brainstorming", "description": "Ж1"},
        {"id": "p:background", "description": "Ж2"},
    ]
    palette = _skill_palette("b", skills, commands=("buffer",))
    plain = _skill_palette("b", [], commands=("buffer",))
    assert palette._hotkey_col_w() == plain._hotkey_col_w()
    rows = [palette._format_skill_row(s).plain for s in skills]
    assert rows[0].startswith(" /brainstorming   ")
    assert rows[1].startswith(" /background      ")
    # описания скиллов выровнены между собой
    assert rows[0].index("Ж1") == rows[1].index("Ж2")


def test_skill_row_name_30_chars_shown_fully():
    pytest.importorskip("textual")
    skill = {"id": "p:b" + "x" * 28, "description": "d"}  # "/b…" = 30
    palette = _skill_palette("b", [skill])
    text = palette._format_skill_row(skill)
    assert text.plain == " /b" + "x" * 28 + "   d"


def test_skill_row_name_40_chars_capped_at_32_with_ellipsis():
    pytest.importorskip("textual")
    skill = {"id": "p:b" + "x" * 38, "description": "d"}  # "/b…" = 40
    palette = _skill_palette("b", [skill])
    text = palette._format_skill_row(skill)
    assert text.plain == " /b" + "x" * 29 + "…" + "   d"
    assert palette._name_col_w() == 32 + 3


def test_skill_row_selection_and_toggle_offset():
    pytest.importorskip("textual")
    from gc_tui.commands import build_default_registry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = build_default_registry()
    skills = [{"id": "p:gzz-a"}, {"id": "p:gzz-b"}]
    palette = CommandPalette(reg, initial_filter="g", skills=lambda: skills)
    commands = palette._visible_rows()
    toggles = palette._visible_toggle_rows()
    assert toggles  # /gate — тоггл, проверяем смещение подвала
    palette._selected = len(commands)
    assert palette.selected_command() is None
    assert palette.selected_skill() == skills[0]
    palette._selected = len(commands) + len(skills)
    assert palette.selected_command() == toggles[0]
    assert palette.selected_skill() is None


def test_desired_height_counts_skill_rows_and_more_line():
    pytest.importorskip("textual")
    skills = [{"id": f"p:brai-{i}"} for i in range(7)]
    plain = _skill_palette("brai", [])
    palette = _skill_palette("brai", skills)
    assert palette.desired_height == plain.desired_height + 5 + 1


def test_cold_skills_cache_shows_no_skills():
    pytest.importorskip("textual")
    from gc_tui.commands import build_default_registry
    from gc_tui.widgets.command_palette import CommandPalette

    palette = CommandPalette(build_default_registry(), initial_filter="b")
    assert palette._visible_skill_rows() == []


@pytest.mark.asyncio
async def test_skill_rows_rendered_after_commands_in_skill_color(tmp_path):
    pytest.importorskip("textual")
    from gc_tui import palette as _pal
    from gc_tui.app import TuiApp
    from textual.color import Color

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(100, 40)) as pilot:
        palette = _skill_palette("b", [_BRAIN, {"id": "p:bzz"}])
        await app.screen.mount(palette)
        await pilot.pause()
        kinds = [
            "skill" if w.has_class("palette-skill-row") else "cmd"
            for w in palette.children
            if w.has_class("palette-row")
        ]
        assert kinds == ["cmd", "skill", "skill"]
        skill_rows = list(palette.query(".palette-skill-row"))
        assert not skill_rows[1].has_class("selected")
        assert skill_rows[1].styles.color == Color.parse(_pal.SKILL)
        assert palette.region.height == palette.desired_height


@pytest.mark.asyncio
async def test_skill_more_line_rendered_not_selectable(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    skills = [{"id": f"p:brai-{i}"} for i in range(9)]
    async with app.run_test(size=(100, 40)) as pilot:
        palette = _skill_palette("brai", skills)
        palette.styles.max_height = 10  # бюджет мал — пол в 5 скиллов
        await app.screen.mount(palette)
        await pilot.pause()
        more = palette.query_one(".palette-skill-more")
        assert str(more.render()) == "… ещё 4 — /skill brai"
        assert len(palette._selectable_rows()) == 5
        assert palette.region.height == palette.desired_height


@pytest.mark.asyncio
async def test_more_line_fits_inside_budget_without_scroll(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(100, 40)) as pilot:
        # 14 = 10 строк списка + рамка(2) + подвал(2)
        palette, _ = _budget_palette("brai", 12, 14, commands=("brai",))
        await app.screen.mount(palette)
        await pilot.pause()
        assert len(palette.query(".palette-skill-row")) == 8
        more = palette.query_one(".palette-skill-more")
        assert str(more.render()) == "… ещё 4 — /skill brai"
        assert palette.region.height == palette.desired_height == 14
        assert not palette.show_vertical_scrollbar


def test_command_row_selected_has_no_skill():
    pytest.importorskip("textual")
    palette = _skill_palette("b", [_BRAIN])
    assert palette.selected_command().name == "branch"
    assert palette.selected_skill() is None


@pytest.mark.asyncio
async def test_palette_shows_skills_from_warmed_screen_cache(
    tmp_path,
    monkeypatch,
    skills_warm,  # noqa: ARG001  # фикстура-прогрев
):
    """Прод-провайдер: палитра берёт скиллы из прогретого кэша экрана."""
    pytest.importorskip("textual")
    from gc_core.tools.skills import registry as reg_mod

    class _Reg:
        async def list_skills(self, *, pack=None, include_all=False):
            return [_BRAIN]

    monkeypatch.setattr(
        reg_mod, "get_skills_registry", lambda cfg=None: _Reg()
    )
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        await wait_until(
            pilot, lambda: app.screen._skills_cache, "прогрев скиллов"
        )
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/brai"
        await pilot.pause(0.1)
        rows = pilot.app.query(".palette-skill-row")
        assert ["/brainstorming" in str(r.render()) for r in rows] == [True]


def test_skill_row_hint_promises_insert_not_run():
    """Enter на строке скилла вставляет `/имя ` — подсказка не обещает
    «выполнить»."""
    pytest.importorskip("textual")
    from gc_tui.widgets.command_palette import _HINT_SKILL

    palette = _skill_palette("brai", [_BRAIN])
    assert palette.selected_skill() == _BRAIN
    assert palette._selected_hint() == _HINT_SKILL
    assert "выполнить" not in _HINT_SKILL


async def _enter_on_skill_row(tmp_path, skills, typed):
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = app.screen
        scr._skills_cache = list(skills)
        inp = pilot.app.query_one("#cmdinput")
        inp.text = typed
        await pilot.pause(0.1)
        sel = scr._palette.selected_skill()
        assert sel["id"] == skills[0]["id"]
        shown = scr._palette._format_skill_row(sel).plain.split()[0]
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert scr._palette is None
        assert inp.text == f"{shown} "  # показано == вставлено
        assert app.tui_state.pending_skill_id is None
        assert not _running(app)
        return inp.text, inp.cursor_location


@pytest.mark.asyncio
async def test_enter_on_skill_row_inserts_short_name(tmp_path):
    pytest.importorskip("textual")
    text, cursor = await _enter_on_skill_row(tmp_path, [_BRAIN], "/brai")
    assert text == "/brainstorming "
    assert cursor == (0, len(text))


@pytest.mark.asyncio
async def test_enter_on_ambiguous_skill_row_inserts_full_id(tmp_path):
    pytest.importorskip("textual")
    skills = [{"id": "a:brainy"}, {"id": "b:brainy"}]
    text, _ = await _enter_on_skill_row(tmp_path, skills, "/brai")
    assert text == "/a:brainy "


def _running(app) -> bool:
    runner = app.active_runner
    return runner is not None and runner.is_running()


@pytest.mark.asyncio
async def test_tab_on_skill_row_inserts_runnable_form(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = app.screen
        scr._skills_cache = [_BRAIN]
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/brai"
        await pilot.pause(0.1)
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert scr._palette is None
        assert inp.text == "/brainstorming "
        assert app.tui_state.pending_skill_id is None


_LONG_SKILL = {"id": "p:s" + "y" * 40, "description": "Скилл с длинным именем"}


def _wide_palette(tmp_path):
    from gc_tui.app import TuiApp
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(
        Command(
            "sessions",
            (),
            "",
            "Подключение, модели, профили, MCP, голос, режимы и прочее",
            hotkey="F2 · Ctrl+S",
        )
    )
    reg.register(Command("stop", (), "", "Остановить задачу", hotkey="Ctrl+C"))
    reg.register(Command("status", (), "", "Состояние"))
    skills = [_LONG_SKILL, {"id": "p:sho", "description": "Короткий скилл"}]
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    pal = CommandPalette(reg, initial_filter="s", skills=lambda: skills)
    return app, pal


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [80, 100, 140])
async def test_palette_layout_hotkeys_whole_one_shared_description_column(
    tmp_path, width
):
    pytest.importorskip("textual")
    app, pal = _wide_palette(tmp_path)
    async with app.run_test(size=(width, 40)) as pilot:
        await app.screen.mount(pal)
        await pilot.pause()
        lines = [
            str(r.render())
            for r in pal.query(".palette-row")
            if not r.has_class("palette-toggle-row")
        ]
        assert len(lines) == 5
        for r in pal.query(".palette-row"):
            assert r.region.height == 1
        joined = "\n".join(lines)
        assert "F2 · Ctrl+S" in joined and "Ctrl+C" in joined
        # хоткеи у правого края: одинаковый правый конец у строк с ними
        ends = {
            len(ln.rstrip())
            for ln in lines
            if ln.rstrip().endswith(("Ctrl+S", "Ctrl+C"))
        }
        assert len(ends) == 1
        # описания всех строк (команд и скиллов) — с одной колонки
        starts = set()
        for ln in lines:
            rest = ln[1 + len(ln.split()[0]) :]
            starts.add(len(ln) - len(rest.lstrip()))
        assert len(starts) == 1
        long_row = next(ln for ln in lines if "/sy" in ln)
        name = long_row.split()[0]
        assert len(name) == 32 and name.endswith("…")


def test_settings_description_is_short():
    from gc_tui.commands import build_default_registry

    cmd = build_default_registry().lookup("settings")
    assert cmd.description == (
        "Подключение, модели, профили, MCP, голос, режимы, внешний вид"
    )


def _main_app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path / "t"),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )


def _row_text(app, row) -> str:
    """Текст строки палитры так, как он реально нарисован на экране."""
    region = row.content_region
    strip = app.screen._compositor.render_strips()[region.y]
    line = "".join(seg.text for seg in strip)
    return line[region.x : region.right]


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [90, 100, 120])
async def test_full_palette_with_scrollbar_keeps_hotkeys_whole(
    tmp_path, width
):
    pytest.importorskip("textual")
    async with _main_app(tmp_path).run_test(size=(width, 24)) as pilot:
        await pilot.pause(0.3)
        scr = pilot.app.screen
        scr._open_palette(prefix="")
        await pilot.pause(0.5)
        pal = scr._palette
        assert pal.show_vertical_scrollbar  # список длиннее окна
        rows = list(pal.query(".palette-row").exclude(".palette-toggle-row"))
        with_hotkey = [
            (row, cmd)
            for row, cmd in zip(rows, pal._visible_rows())
            if cmd.hotkey
        ]
        assert len(with_hotkey) >= 4
        for row, cmd in with_hotkey:
            pal.scroll_to(y=row.virtual_region.y, animate=False)
            await pilot.pause(0.05)
            assert row.region.height == 1
            assert _row_text(pilot.app, row).rstrip().endswith(cmd.hotkey)


def test_skill_rows_alphabetical_by_short_name():
    """Порядок строк скиллов — по короткому имени без регистра, не по
    слоям реестра (пакет в строке палитры не виден)."""
    pytest.importorskip("textual")
    skills = [
        {"id": "zeta:bz-b"},
        {"id": "bz-c"},
        {"id": "alpha:bz-a"},
        {"id": "bz-d"},
    ]
    palette = _skill_palette("bz", skills)
    assert [s["id"] for s in palette._visible_skill_rows()] == [
        "alpha:bz-a",
        "zeta:bz-b",
        "bz-c",
        "bz-d",
    ]


@pytest.mark.parametrize("filter_", ["bz", "BZ", "p:bz", "P:BZ"])
def test_skill_match_case_insensitive_on_both_sides(filter_):
    """Имя `Bz-a` находится и по `/bz`, и по `/BZ`; полный id — тоже."""
    pytest.importorskip("textual")
    skill = {"id": "P:Bz-a"}
    palette = _skill_palette(filter_, [skill])
    assert palette._visible_skill_rows() == [skill]


# --- Алиасы и раскладка в фильтре --------------------------------------------


def _filtered_palette(typed: str, skills=None):
    pytest.importorskip("textual")
    from gc_tui.commands import build_default_registry
    from gc_tui.widgets.command_palette import CommandPalette

    return CommandPalette(
        build_default_registry(), initial_filter=typed, skills=skills
    )


def _cmd_row_text(palette, name: str) -> str:
    cmd = next(c for c in palette._visible_rows() if c.name == name)
    return str(palette._format_row(cmd))


def test_alias_match_shows_alias_in_brackets():
    palette = _filtered_palette("cle")
    assert [c.name for c in palette._visible_rows()] == ["new"]
    assert "/new (clear)" in _cmd_row_text(palette, "new")


def test_name_match_shows_no_alias():
    palette = _filtered_palette("ne")
    assert [c.name for c in palette._visible_rows()] == ["new"]
    assert "(" not in _cmd_row_text(palette, "new").split("Новая")[0]


def test_name_matches_listed_before_alias_matches():
    palette = _filtered_palette("c")
    assert [c.name for c in palette._visible_rows()] == [
        "config",
        "copy",
        "compact",
        "sessions",
        "new",
        "stop",
    ]


def test_other_layout_name_match_is_a_plain_name_match():
    palette = _filtered_palette("ьщв")
    assert [c.name for c in palette._visible_rows()] == ["model"]
    assert "(" not in _cmd_row_text(palette, "model").split("Модель")[0]


def test_other_layout_alias_match_shows_real_alias():
    palette = _filtered_palette("c;f")
    assert [c.name for c in palette._visible_rows()] == ["compact"]
    assert "/compact (сжать)" in _cmd_row_text(palette, "compact")


def test_alias_label_widens_name_column():
    from gc_tui.widgets.palette_layout import name_col_w

    palette = _filtered_palette("cont")
    assert palette._name_col_w() == name_col_w(["/sessions (continue)"])


def test_toggle_alias_match_shows_alias():
    palette = _filtered_palette("гейт")
    rows = palette._visible_toggle_rows()
    gate = next(c for c in rows if c.name == "gate")
    assert "/gate (гейт)" in palette._row_head(gate)


def test_other_layout_only_when_typed_finds_nothing():
    palette = _filtered_palette("c")
    assert "help" not in [c.name for c in palette._visible_rows()]


def test_skill_matches_in_other_layout():
    skills = [{"id": "pack:brainstorming"}]
    palette = _filtered_palette("икфшт", skills=lambda: skills)
    assert palette._visible_skill_rows() == skills


def test_skill_more_hint_uses_filter_in_right_layout():
    skills = [{"id": "pack:brainstorming"}]
    palette = _filtered_palette("икфшт", skills=lambda: skills)
    assert palette._skill_more_text().endswith("/skill brain")


def test_filter_layout_checked_once_per_typed_text():
    calls = []

    def skills():
        calls.append(1)
        return [{"id": "pack:brainstorming"}]

    palette = _filtered_palette("zzz", skills=skills)
    before = len(calls)
    for cmd in palette._registry.list_commands():
        palette._label(cmd)
    assert len(calls) - before <= 1


@pytest.mark.parametrize(
    "typed, word",
    [
        ("ne", "new"),
        ("cle", "clear"),
        ("очи", "new"),
        ("ту", "new"),
        ("сду", "clear"),
    ],
)
def test_completion_is_the_typed_word(typed, word):
    """Tab дописывает набираемое слово латиницей: имя, совпавший
    английский синоним или то же слово в правильной раскладке; русское
    слово — командой, к которой оно привязано."""
    palette = _filtered_palette(typed)
    new = next(c for c in palette._visible_rows() if c.name == "new")
    assert palette.completion(new) == word


@pytest.mark.asyncio
async def test_tab_completes_alias_as_typed(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        for ch in "/cle":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert inp.text == "/clear "


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "typed, inserted", [("/models", "/models "), ("/модел", "/model ")]
)
async def test_enter_inserts_command_word_like_tab(tmp_path, typed, inserted):
    """Enter, подставляющий команду с аргументом, пишет слово по тому же
    правилу, что Tab: английский синоним — как набран, русское слово —
    командой."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        inp.insert(typed)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert inp.text == inserted


@pytest.mark.parametrize("typed", ["reas", "рассужд"])
def test_reasoning_toggle_found_by_name_and_alias(typed):
    palette = _filtered_palette(typed)
    assert [c.name for c in palette._visible_toggle_rows()] == ["reasoning"]


def test_reasoning_toggle_absent_in_empty_palette():
    palette = _filtered_palette("")
    names = [c.name for c in palette._visible_toggle_rows()]
    assert "reasoning" not in names
