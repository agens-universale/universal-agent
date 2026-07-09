"""ArgPicker integration в TraceScreen: триггер на /export <space>, фильтр, Tab,
Enter (выполняет export), Esc, mutex с командной палитрой."""
import pytest

from thread_store import append_new_thread_messages
from tui.services.stores.file_store import FileTaskStore


def _mk_app(tmp_path):
    from tui.app import TuiApp
    return TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})


def test_export_command_has_arg_options():
    from tui.commands import build_default_registry
    export = build_default_registry().lookup("export")
    assert export.arg_options == (
        ("txt", "вся сессия в читаемом виде"),
        ("json", "структурированные данные сессии"),
        ("log-txt", "лог-файл агента, текущий запуск (.log)"),
        ("log-json", "jsonl-файл треда (вся сессия)"),
    )


@pytest.mark.asyncio
async def test_picker_opens_on_export_space(tmp_path):
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker
    async with _mk_app(tmp_path).run_test() as pilot:
        pilot.app.query_one("#cmdinput").text = "/export "
        await pilot.pause(0.1)
        assert len(pilot.app.query(ArgPicker)) == 1
        rows = "\n".join(
            str(s.render()) for s in pilot.app.query_one(ArgPicker).query(".palette-row")
        )
        assert "txt" in rows and "json" in rows


@pytest.mark.asyncio
async def test_picker_filters_by_prefix(tmp_path):
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker
    async with _mk_app(tmp_path).run_test() as pilot:
        pilot.app.query_one("#cmdinput").text = "/export j"
        await pilot.pause(0.1)
        rows = "\n".join(
            str(s.render()) for s in pilot.app.query_one(ArgPicker).query(".palette-row")
        )
        assert "json" in rows
        assert "txt" not in rows


@pytest.mark.asyncio
async def test_tab_completes_value_and_closes(tmp_path):
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker
    async with _mk_app(tmp_path).run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/export j"
        await pilot.pause(0.1)
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert inp.text == "/export json"
        assert len(pilot.app.query(ArgPicker)) == 0


@pytest.mark.asyncio
async def test_enter_runs_export_with_selected_value(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    append_new_thread_messages(str(tmp_path), "cli", "tidX",
                               [{"role": "user", "content": "q", "id": "m1"}], set())
    from tui.widgets.arg_picker import ArgPicker
    async with _mk_app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "tidX"
        scr._store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/export json"
        await pilot.pause(0.1)
        assert len(pilot.app.query(ArgPicker)) == 1
        await pilot.press("enter")
        await pilot.pause(0.1)
        files = list(tmp_path.glob("export-tidX-*.json"))
        assert len(files) == 1


@pytest.mark.asyncio
async def test_esc_closes_keeps_input(tmp_path):
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker
    async with _mk_app(tmp_path).run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/export j"
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert len(pilot.app.query(ArgPicker)) == 0
        assert inp.text == "/export j"


@pytest.mark.asyncio
async def test_mutex_with_command_palette(tmp_path):
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker
    from tui.widgets.command_palette import CommandPalette
    async with _mk_app(tmp_path).run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/exp"
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 1
        assert len(pilot.app.query(ArgPicker)) == 0

        inp.text = "/export "
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 0
        assert len(pilot.app.query(ArgPicker)) == 1

        inp.text = "/export"
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 1
        assert len(pilot.app.query(ArgPicker)) == 0
