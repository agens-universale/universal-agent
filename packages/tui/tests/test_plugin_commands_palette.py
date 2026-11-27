"""Команды плагинов в палитре: строка как у скилла, хвост `· <плагин>`,
имя — короткое, если однозначно по командам и скиллам (§4.2)."""

import pytest
from gc_core.plugins.catalog import plugin_fingerprint
from gc_core.plugins.manifest import PluginCommand
from gc_core.plugins.snapshot import PluginSnapshot
from gc_testkit.tui_support import tui_settings, wait_until


def _cmd(name, description="ревью изменений"):
    return PluginCommand(
        name=name,
        description=description,
        argument_hint="",
        allowed_tools=(),
        body="BODY $ARGUMENTS",
        source=f"/p/commands/{name}.md",
    )


def _app(tmp_path, commands):
    from gc_tui.app import TuiApp

    settings = tui_settings({"agent.model": "M"})
    snap = PluginSnapshot(
        commands=tuple(commands), fingerprint=plugin_fingerprint(settings)
    )
    settings = settings.with_runtime(plugins=snap)
    return TuiApp(threads_dir=str(tmp_path), system="cli", settings=settings)


async def _skill_rows(pilot, skills, typed):
    scr = pilot.app.screen
    scr._skills_cache = skills
    await pilot.press("/")
    for ch in typed:
        await pilot.press(ch)
    palette = pilot.app.query_one(".palette")
    await wait_until(
        pilot, lambda: palette.query(".palette-skill-row"), "skill rows"
    )
    return [str(r.render()) for r in palette.query(".palette-skill-row")]


@pytest.mark.asyncio
async def test_plugin_command_row_styled_as_skill_with_plugin_tail(tmp_path):
    pytest.importorskip("textual")
    app = _app(tmp_path, [("code-tools", _cmd("review"))])
    async with app.run_test() as pilot:
        rows = await _skill_rows(pilot, [{"id": "other:x"}], "re")
        assert len(rows) == 1
        assert rows[0].startswith(" /review ")
        assert "ревью изменений" in rows[0]
        assert rows[0].rstrip().endswith("· code-tools")


@pytest.mark.asyncio
async def test_plugin_command_ambiguous_with_skill_shows_full_names(
    tmp_path,
):
    pytest.importorskip("textual")
    app = _app(tmp_path, [("code-tools", _cmd("review"))])
    async with app.run_test() as pilot:
        rows = await _skill_rows(
            pilot, [{"id": "pack:review", "description": "скилл"}], "re"
        )
        names = sorted(r.split()[0] for r in rows)
        assert names == ["/code-tools:review", "/pack:review"]


@pytest.mark.asyncio
async def test_enter_on_plugin_command_row_inserts_runnable_name(tmp_path):
    pytest.importorskip("textual")
    app = _app(tmp_path, [("code-tools", _cmd("review"))])
    async with app.run_test() as pilot:
        await _skill_rows(pilot, [], "rev")
        await pilot.press("enter")
        assert pilot.app.query_one("#cmdinput").text == "/review "


def test_skill_input_name_full_id_when_plugin_command_shares_short():
    from gc_core.tools.skills.skill_arg_resolver import catalog_from_entries
    from gc_tui.services.skill_view import skill_input_name

    catalog = catalog_from_entries([{"id": "pack:review"}])
    commands = (("code-tools", _cmd("review")),)
    assert skill_input_name("pack:review", catalog) == "review"
    assert (
        skill_input_name("pack:review", catalog, commands=commands)
        == "pack:review"
    )


def test_plugin_command_rows_short_unless_taken():
    from gc_tui.services.skill_view import plugin_command_rows

    commands = (("a", _cmd("go", "d")), ("b", _cmd("go")), ("a", _cmd("x")))
    rows = plugin_command_rows(
        commands, ["s:x"], is_command=lambda name: False
    )
    assert [(r["id"], r["input_name"], r["plugin"]) for r in rows] == [
        ("a:go", "a:go", "a"),
        ("b:go", "b:go", "b"),
        ("a:x", "a:x", "a"),
    ]
    rows = plugin_command_rows(commands[:1], [], is_command=lambda name: False)
    assert rows == [
        {"id": "a:go", "description": "d", "input_name": "go", "plugin": "a"}
    ]
    taken = plugin_command_rows(
        commands[:1], [], is_command=lambda name: name == "go"
    )
    assert taken[0]["input_name"] == "a:go"


@pytest.mark.asyncio
async def test_ambiguity_with_plugin_command_says_commands_and_skills(
    tmp_path,
):
    pytest.importorskip("textual")
    app = _app(tmp_path, [("code-tools", _cmd("review"))])
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "pack:review"}]
        await scr._handle_command("review main.py")
        await pilot.pause(0.2)
        text = " ".join(
            str(w.render()) for w in scr.query(".line-error").results()
        )
        assert "несколько команд и скиллов с именем: /review" in text
        assert "/code-tools:review" in text and "/pack:review" in text
        assert "несколько скиллов" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["clear", "x:cl"])
async def test_plugin_command_shadowed_by_tui_word_listed_by_full_id(
    tmp_path, typed
):
    """Команда `x:clear` при алиасе TUI `clear`: строка плагина видна и по
    короткому слову (ниже `/new`), и по полному id; вставляет полный id."""
    pytest.importorskip("textual")
    app = _app(tmp_path, [("x", _cmd("clear"))])
    async with app.run_test() as pilot:
        rows = await _skill_rows(pilot, [{"id": "other:zzz"}], typed)
        assert [r.split()[0] for r in rows] == ["/x:clear"]
        palette = pilot.app.screen._palette
        commands = [c.name for c in palette._visible_rows()]
        assert commands == (["new"] if typed == "clear" else [])
