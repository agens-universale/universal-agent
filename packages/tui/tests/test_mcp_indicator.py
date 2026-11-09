from gc_testkit.tui_support import tui_settings
import pytest

_OFFICE = {
    "mcp.servers": {"office": {"transport": "stdio", "command": "office-mcp"}}
}


def _header_text(app) -> str:
    """Шапка так, как её раскладывает сам виджет по своей ширине."""
    header = app.screen.query_one("#brand-block")
    return header.render().laid_out(header.content_region.width).plain


def _status(app) -> str:
    return _header_text(app).splitlines()[-1]


def _app(tmp_path):
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M", **_OFFICE}),
    )
    # Тест управляет app._mcp_progress сам; без стаба warm() из on_mount строит
    # реальный executor и перезаписывает sink (_build_real_executor) — гонка/флак.
    app._executor_factory = lambda: object()
    return app


def _failed_office(*, finished: bool):
    from gc_tui.services.mcp_progress import McpProgress

    prog = McpProgress()
    prog.begin(["office"])
    prog.update("office", {"status": "failed", "reason": "timeout"})
    if finished:
        prog.finish(True)
    return prog


def _spin(app, ticks: int = 30) -> None:
    header = app.screen.query_one("#brand-block")
    for _ in range(ticks):
        header._tick()


@pytest.mark.asyncio
async def test_failed_server_passes_the_peephole_then_warns(tmp_path):
    pytest.importorskip("textual")
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        scr = pilot.app.screen
        prog = _failed_office(finished=False)
        app._mcp_progress = prog
        scr._feed_start_header()
        _spin(app, 1)
        await pilot.pause()
        # упавший сервер виден в «глазке», счётчик подключившихся не растёт
        assert _status(app) == "• подключаю MCP 0/1 · office ✗"
        prog.finish(True)
        scr._feed_start_header()
        _spin(app)
        await pilot.pause()
        assert _status(app) == "✓ готов к работе   ⚠ MCP 0/1 · /mcp"
        assert app._mcp_progress is prog  # sink экран не гасит


@pytest.mark.asyncio
async def test_indicator_warning_survives_trace_rebuild(tmp_path):
    """Итог загрузки MCP — часть стартовой шапки (как бренд): переживает
    пересборку #trace (первое сообщение / new / resume / stream_reset)."""
    pytest.importorskip("textual")
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        scr = pilot.app.screen
        app._mcp_progress = _failed_office(finished=True)
        scr._feed_start_header()
        _spin(app)
        await pilot.pause()
        assert "⚠ MCP 0/1 · /mcp" in _status(app)
        # Пересборка трейса (все точки идут через _clear_trace)
        scr._switch_to_empty_state()
        await pilot.pause()
        assert len(pilot.app.query("#brand-block")) == 1
        assert "⚠ MCP 0/1 · /mcp" in _status(app)
        trace = pilot.app.query_one("#trace")
        assert list(trace.children)[0].id == "brand-block"


@pytest.mark.asyncio
async def test_settled_header_has_no_notices_and_trace_is_clean(tmp_path):
    """После итога в шапке только готовность и MCP: бейдж уведомлений — в
    футере; в трейсе одна строка готовности и нет блока «Уведомления»."""
    pytest.importorskip("textual")
    from textual.widgets import Static

    from gc_tui.services.notices_ui import WARN_GLYPH

    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        scr = pilot.app.screen
        # mcp_status оседает к итогу — эмулируем упавший office.
        app._mcp_status = {"office": {"status": "failed", "reason": "timeout"}}
        app._mcp_progress = _failed_office(finished=True)
        scr._feed_start_header()
        _spin(app)
        scr._refresh_footer()
        await pilot.pause()
        header = _header_text(app)
        assert "F4/notices" not in header
        footer = str(scr.query_one("#footer-slot").render())
        assert f"{WARN_GLYPH} 1" in footer and "F4/notices" in footer
        trace = pilot.app.query_one("#trace")
        texts = [str(s.render()) for s in trace.query(Static)] + [header]
        assert len([t for t in texts if "готов" in t]) == 1
        assert not any("Уведомления ·" in t for t in texts)
