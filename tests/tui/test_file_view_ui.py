"""Smoke: `/view` открывает FileViewerScreen; пагинация и позиция."""
import pytest
from textual.widgets import Static


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
        body = str(pilot.app.screen.query_one("#file-view-body", Static).render())
        assert "line-1" in body
        left = str(pilot.app.screen.query_one("#header-left", Static).render())
        assert "view ·" in left


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
        body = str(pilot.app.screen.query_one("#file-view-body", Static).render())
        assert "файл не найден" in body


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
        body = str(pilot.app.screen.query_one("#file-view-body", Static).render())
        assert "использование: /view" in body


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
        body = str(viewer.query_one("#file-view-body", Static).render())
        assert "L50" in body
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, FileViewerScreen)

        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        viewer2 = pilot.app.screen
        assert isinstance(viewer2, FileViewerScreen)
        assert viewer2._start_line == 50
        body2 = str(viewer2.query_one("#file-view-body", Static).render())
        assert "L50" in body2


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
        body_lines = str(viewer.query_one("#file-view-body", Static).render()).splitlines()
        assert body_lines[0] == "R10"
        assert "R1" not in body_lines
