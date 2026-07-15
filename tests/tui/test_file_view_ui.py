"""Smoke: `/view` открывает FileViewerScreen; пагинация и позиция."""
import pytest
from textual.widgets import Static, TextArea
from textual.widgets.text_area import Selection


@pytest.mark.asyncio
async def test_view_command_opens_file(tmp_path):
    pytest.importorskip("textual")
    f = tmp_path / "sample.txt"
    f.write_text("\n".join(f"line-{i}" for i in range(1, 41)) + "\n", encoding="utf-8")

    from tui.app import TuiApp
    from tui.screens.file_view import FileViewerScreen

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        # cwd TUI = процесс cwd; подменим через _cmd_view напрямую с tmp_path
        scr._cmd_view(str(f))
        await pilot.pause()
        assert isinstance(pilot.app.screen, FileViewerScreen)
        body = pilot.app.screen.query_one("#file-view-body", TextArea)
        assert "line-1" in body.text
        left = str(pilot.app.screen.query_one("#header-left", Static).render())
        assert "view ·" in left


@pytest.mark.asyncio
async def test_view_python_uses_syntax_highlight(tmp_path):
    pytest.importorskip("textual")
    f = tmp_path / "mod.py"
    f.write_text("def hello():\n    return 1\n", encoding="utf-8")

    from tui.app import TuiApp
    from tui.screens.file_view import FileViewerScreen

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, FileViewerScreen)
        assert viewer._lexer == "python"
        body = viewer.query_one("#file-view-body", TextArea)
        assert "def hello():" in body.text
        # Без textual[syntax] подсветка через pygments → _highlights.
        styles = {name for spans in body._highlights.values() for _, _, name in spans}
        assert "keyword" in styles or "keyword.function" in styles
        assert any(styles)  # не пустая карта


@pytest.mark.asyncio
async def test_view_missing_file_error_on_screen(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.file_view import FileViewerScreen

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        pilot.app.screen._cmd_view(str(tmp_path / "no-such.txt"))
        await pilot.pause()
        assert isinstance(pilot.app.screen, FileViewerScreen)
        body = pilot.app.screen.query_one("#file-view-body", TextArea)
        assert "файл не найден" in body.text


@pytest.mark.asyncio
async def test_view_usage_without_path(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.file_view import FileViewerScreen

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        pilot.app.screen._cmd_view("")
        await pilot.pause()
        assert isinstance(pilot.app.screen, FileViewerScreen)
        body = pilot.app.screen.query_one("#file-view-body", TextArea)
        assert "использование: /view" in body.text


@pytest.mark.asyncio
async def test_view_remembers_page_across_reopen(tmp_path):
    pytest.importorskip("textual")
    f = tmp_path / "long.txt"
    f.write_text("\n".join(f"L{i}" for i in range(1, 201)) + "\n", encoding="utf-8")

    from tui.app import TuiApp
    from tui.screens.file_view import FileViewerScreen

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, FileViewerScreen)
        viewer._page = 10
        viewer._start_line = 50
        viewer._reload_window()
        await pilot.pause()
        body = viewer.query_one("#file-view-body", TextArea)
        assert "L50" in body.text
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, FileViewerScreen)

        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        viewer2 = pilot.app.screen
        assert isinstance(viewer2, FileViewerScreen)
        assert viewer2._start_line == 50
        body2 = viewer2.query_one("#file-view-body", TextArea)
        assert "L50" in body2.text


@pytest.mark.asyncio
async def test_view_arrow_hold_coalesces_moves(tmp_path):
    """Зажатая ↓ не должна делать read на каждый key-repeat — throttle ~10 Hz."""
    pytest.importorskip("textual")
    import asyncio

    f = tmp_path / "thr.txt"
    f.write_text("\n".join(f"T{i}" for i in range(1, 201)) + "\n", encoding="utf-8")

    from tui.app import TuiApp
    from tui.screens.file_view import FileViewerScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        config={"tui_file_view": {"page_lines": 10}},
    )
    async with app.run_test() as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, FileViewerScreen)
        assert viewer._start_line == 1

        for _ in range(10):
            viewer.action_line_down()
        # leading-edge: один flush сразу, остальное в pending
        assert viewer._start_line == 2
        assert viewer._pending_delta == 9

        await asyncio.sleep(0.12)
        await pilot.pause()
        assert viewer._pending_delta == 0
        assert viewer._start_line == 11


@pytest.mark.asyncio
async def test_view_pagedown_changes_window(tmp_path):
    pytest.importorskip("textual")
    f = tmp_path / "p.txt"
    f.write_text("\n".join(f"R{i}" for i in range(1, 101)) + "\n", encoding="utf-8")

    from tui.app import TuiApp
    from tui.screens.file_view import FileViewerScreen
    from tui.services.file_view import FileViewSettings

    app = TuiApp(
        threads_dir=str(tmp_path),
        config={"tui_file_view": {"page_lines": 10}},
    )
    async with app.run_test() as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, FileViewerScreen)
        assert viewer._page == 10
        assert viewer._settings == FileViewSettings.from_mapping({"page_lines": 10})
        await pilot.press("pagedown")
        await pilot.pause()
        # после page down с overlap 1: start = 1 + (10-1) = 10
        assert viewer._start_line == 10
        body_lines = viewer.query_one("#file-view-body", TextArea).text.splitlines()
        assert body_lines[0] == "R10"
        assert "R1" not in body_lines


@pytest.mark.asyncio
async def test_view_get_selected_text_returns_window_lines(tmp_path):
    """Ctrl+C без мышиного выделения — fallback на текст текущей страницы."""
    pytest.importorskip("textual")
    f = tmp_path / "lines.txt"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    from tui.app import TuiApp
    from tui.screens.file_view import FileViewerScreen

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, FileViewerScreen)
        assert viewer.get_selected_text() == "alpha\nbeta\ngamma"


@pytest.mark.asyncio
async def test_view_get_selected_text_uses_mouse_selection(tmp_path):
    """Мышиное/программное выделение в TextArea имеет приоритет над fallback."""
    pytest.importorskip("textual")
    f = tmp_path / "sel.txt"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    from tui.app import TuiApp
    from tui.screens.file_view import FileViewerScreen

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, FileViewerScreen)
        body = viewer.query_one("#file-view-body", TextArea)
        body.selection = Selection((0, 0), (0, 5))
        assert body.selected_text == "alpha"
        assert viewer.get_selected_text() == "alpha"


@pytest.mark.asyncio
async def test_view_get_selected_text_empty_when_no_window(tmp_path):
    """get_selected_text: ошибка файла без _window → "" (текст ошибки не копируем)."""
    pytest.importorskip("textual")

    from tui.app import TuiApp
    from tui.screens.file_view import FileViewerScreen
    from tui.services.file_view import ViewPositionStore

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        viewer = FileViewerScreen(
            raw_path=str(tmp_path / "missing.txt"),
            cwd=str(tmp_path),
            store=ViewPositionStore(),
        )
        app.push_screen(viewer)
        await pilot.pause()
        assert viewer.get_selected_text() == ""


@pytest.mark.asyncio
async def test_view_get_selected_text_after_pagedown(tmp_path):
    """После PgDown get_selected_text возвращает текст новой страницы."""
    pytest.importorskip("textual")
    f = tmp_path / "pages.txt"
    f.write_text("\n".join(f"LINE{i:03d}" for i in range(1, 101)) + "\n", encoding="utf-8")

    from tui.app import TuiApp
    from tui.screens.file_view import FileViewerScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        config={"tui_file_view": {"page_lines": 5}},
    )
    async with app.run_test() as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, FileViewerScreen)
        first_text = viewer.get_selected_text()
        assert "LINE001" in first_text
        assert "LINE005" in first_text

        await pilot.press("pagedown")
        await pilot.pause()
        second_text = viewer.get_selected_text()
        assert "LINE001" not in second_text
        assert "LINE006" in second_text
