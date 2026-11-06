"""Smoke: `/view` открывает FileViewerScreen; пагинация и позиция."""

from gc_testkit.tui_support import tui_settings
import pytest
from textual.widgets import Static, TextArea
from textual.widgets.text_area import Selection


@pytest.fixture
def paged(monkeypatch):
    """Режим окон (большой файл) на маленьком: порог «целиком» — ноль."""
    from gc_tui.services import file_view

    monkeypatch.setattr(file_view, "WHOLE_FILE_MAX_BYTES", 0)


@pytest.mark.asyncio
async def test_view_command_opens_file(tmp_path):
    pytest.importorskip("textual")
    f = tmp_path / "sample.txt"
    f.write_text(
        "\n".join(f"line-{i}" for i in range(1, 41)) + "\n", encoding="utf-8"
    )

    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen

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

    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen
    from gc_tui.services.file_view_theme import FILE_VIEW_THEME

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
        styles = {
            name for spans in body._highlights.values() for _, _, name in spans
        }
        assert "keyword" in styles or "keyword.function" in styles
        assert any(styles)  # не пустая карта
        # Тема подсветки — из палитры, а не встроенная vscode_dark.
        assert body.theme == FILE_VIEW_THEME


@pytest.mark.asyncio
async def test_view_missing_file_error_on_screen(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen

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
    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        pilot.app.screen._cmd_view("")
        await pilot.pause()
        assert isinstance(pilot.app.screen, FileViewerScreen)
        body = pilot.app.screen.query_one("#file-view-body", TextArea)
        assert "использование: /view" in body.text


@pytest.mark.asyncio
async def test_view_remembers_page_across_reopen(tmp_path, paged):
    pytest.importorskip("textual")
    f = tmp_path / "long.txt"
    f.write_text(
        "\n".join(f"L{i}" for i in range(1, 201)) + "\n", encoding="utf-8"
    )

    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen

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
async def test_view_arrow_hold_coalesces_moves(tmp_path, paged):
    """Зажатая ↓ не должна делать read на каждый key-repeat — throttle ~10 Hz."""
    pytest.importorskip("textual")
    import asyncio

    f = tmp_path / "thr.txt"
    f.write_text(
        "\n".join(f"T{i}" for i in range(1, 201)) + "\n", encoding="utf-8"
    )

    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        settings=tui_settings({"tui.file_view": {"page_lines": 10}}),
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
async def test_view_pagedown_changes_window(tmp_path, paged):
    pytest.importorskip("textual")
    f = tmp_path / "p.txt"
    f.write_text(
        "\n".join(f"R{i}" for i in range(1, 101)) + "\n", encoding="utf-8"
    )

    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen
    from gc_tui.services.file_view import FileViewSettings

    app = TuiApp(
        threads_dir=str(tmp_path),
        settings=tui_settings({"tui.file_view": {"page_lines": 10}}),
    )
    async with app.run_test() as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, FileViewerScreen)
        assert viewer._page == 10
        assert viewer._settings == FileViewSettings.from_mapping(
            {"page_lines": 10}
        )
        await pilot.press("pagedown")
        await pilot.pause()
        # после page down с overlap 1: start = 1 + (10-1) = 10
        assert viewer._start_line == 10
        body_lines = viewer.query_one(
            "#file-view-body", TextArea
        ).text.splitlines()
        assert body_lines[0] == "R10"
        assert "R1" not in body_lines


@pytest.mark.asyncio
async def test_view_get_selected_text_returns_window_lines(tmp_path):
    """Ctrl+C без мышиного выделения — fallback на текст текущей страницы."""
    pytest.importorskip("textual")
    f = tmp_path / "lines.txt"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, FileViewerScreen)
        assert viewer.get_selected_text() == "alpha\nbeta\ngamma"


@pytest.mark.asyncio
async def test_view_get_selected_text_uses_mouse_selection(
    tmp_path, monkeypatch
):
    """Мышиное/программное выделение в TextArea имеет приоритет над fallback."""
    pytest.importorskip("textual")
    f = tmp_path / "sel.txt"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen

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
        # cwd экрана = каталог файла → относительный cite (как у diff).
        viewer._cwd = str(tmp_path)
        assert viewer.clipboard_source_cite() == ("file", "sel.txt")
        monkeypatch.setattr(pilot.app, "_current_selection", lambda: "alpha")
        assert pilot.app._clipboard_payload() == "alpha\n[file: sel.txt]"


@pytest.mark.asyncio
async def test_view_get_selected_text_empty_when_no_window(tmp_path):
    """get_selected_text: ошибка файла без _window → "" (текст ошибки не копируем)."""
    pytest.importorskip("textual")

    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen
    from gc_tui.services.file_view import ViewPositionStore

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
        assert viewer.clipboard_source_cite() is None


@pytest.mark.asyncio
async def test_view_get_selected_text_after_pagedown(tmp_path, paged):
    """После PgDown get_selected_text возвращает текст новой страницы."""
    pytest.importorskip("textual")
    f = tmp_path / "pages.txt"
    f.write_text(
        "\n".join(f"LINE{i:03d}" for i in range(1, 101)) + "\n",
        encoding="utf-8",
    )

    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        settings=tui_settings({"tui.file_view": {"page_lines": 5}}),
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


@pytest.mark.asyncio
async def test_view_soft_wrap_enabled(tmp_path):
    """Длинная строка переносится по ширине окна: TextArea.soft_wrap is True."""
    pytest.importorskip("textual")
    f = tmp_path / "wrap.txt"
    f.write_text("x" * 400 + "\n", encoding="utf-8")

    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, FileViewerScreen)
        body = viewer.query_one("#file-view-body", TextArea)
        assert body.soft_wrap is True


@pytest.mark.asyncio
async def test_small_file_opens_whole_and_scrolls_natively(tmp_path):
    """Файл до порога — целиком: весь текст в области, полосы позиции нет,
    PgDn прокручивает, шапка — видимые строки."""
    f = tmp_path / "long.txt"
    f.write_text("\n".join(f"W{i}" for i in range(1, 301)) + "\n")

    from gc_tui.app import TuiApp
    from gc_tui.widgets.file_position_bar import FilePositionBar

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test(size=(80, 30)) as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        viewer = pilot.app.screen
        body = viewer.query_one("#file-view-body", TextArea)
        assert body.text.splitlines()[-1] == "W300"
        assert not viewer.query_one(FilePositionBar).display
        await pilot.press("pagedown")
        await pilot.pause()
        assert body.scroll_y > 0
        right = str(viewer.query_one("#header-right", Static).render())
        assert not right.startswith("1–") and right.endswith("/ 300")


@pytest.mark.asyncio
async def test_whole_file_reopens_at_the_same_line(tmp_path):
    f = tmp_path / "long.txt"
    f.write_text("\n".join(f"W{i}" for i in range(1, 301)) + "\n")

    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test(size=(80, 30)) as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        await pilot.press("pagedown", "pagedown")
        await pilot.pause()
        top = pilot.app.screen._start_line
        assert top > 1
        await pilot.press("escape")
        await pilot.pause()
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        await pilot.pause()
        assert pilot.app.screen._start_line == top


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["whole", "paged"])
async def test_reopens_after_file_shrank(tmp_path, monkeypatch, mode):
    """Файл укоротили между открытиями: сохранённая строка за концом —
    просмотр открывается на доступных строках, а не падает (целиком и
    окнами)."""
    if mode == "paged":
        from gc_tui.services import file_view

        monkeypatch.setattr(file_view, "WHOLE_FILE_MAX_BYTES", 0)
    f = tmp_path / "long.txt"
    f.write_text("\n".join(f"W{i}" for i in range(1, 301)) + "\n")

    from gc_tui.app import TuiApp
    from gc_tui.screens.file_view import FileViewerScreen
    from textual.widgets import TextArea

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test(size=(80, 30)) as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        await pilot.press("pagedown", "pagedown")
        await pilot.pause()
        assert pilot.app.screen._start_line > 3
        await pilot.press("escape")
        await pilot.pause()
        f.write_text("W1\nW2\nW3\n")
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, FileViewerScreen)
        body = viewer.query_one("#file-view-body", TextArea)
        assert "W3" in body.text


@pytest.mark.asyncio
async def test_paged_wheel_and_click_move_the_window(tmp_path, paged):
    """Окнами: колесо листает файл, клик по полосе — прыжок в это место."""
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"B{i}" for i in range(1, 1001)) + "\n")

    from gc_tui.app import TuiApp
    from gc_tui.widgets.file_position_bar import FilePositionBar

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test(size=(80, 30)) as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.pause()
        viewer = pilot.app.screen
        bar = viewer.query_one(FilePositionBar)
        assert bar.display
        from textual import events

        body = viewer.query_one("#file-view-body", TextArea)
        body.post_message(
            events.MouseScrollDown(body, 1, 1, 0, 1, 0, False, False, False)
        )
        await pilot.pause()
        assert viewer._start_line == 4
        await pilot.pause(0.15)
        await pilot.click(FilePositionBar, offset=(0, bar.size.height - 1))
        await pilot.pause(0.15)
        assert viewer._start_line > 900


@pytest.mark.asyncio
async def test_paged_counts_unknown_length_in_background(
    tmp_path, paged, monkeypatch
):
    """Длина большого файла неизвестна при открытии — досчитывается в фоне,
    шапка и полоса узнают её."""
    from gc_tui.screens import file_view as screen_mod

    real = screen_mod.open_for_view
    monkeypatch.setattr(
        screen_mod,
        "open_for_view",
        lambda *a, **kw: real(*a, **{**kw, "count_eager_bytes": 0}),
    )
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"B{i}" for i in range(1, 501)) + "\n")

    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test(size=(80, 30)) as pilot:
        pilot.app.screen._cmd_view(str(f))
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        viewer = pilot.app.screen
        assert viewer._total_lines == 500
        right = str(viewer.query_one("#header-right", Static).render())
        assert right.endswith("/ 500")
