"""Стартовая шапка на главном экране: загрузка MCP «глазком», итог, сбой
сборки, хвост обновления и кто вправе писать строку статуса (контракт
2026-11-08-start-header §3, §6)."""

from __future__ import annotations

import pytest
from gc_testkit import as_coro
from gc_testkit.tui_support import tui_settings

pytest.importorskip("textual")

_TWO_SERVERS = {
    "mcp.servers": {
        "alpha": {"transport": "stdio", "command": "alpha-mcp"},
        "beta": {"transport": "stdio", "command": "beta-mcp"},
    }
}
_STATUS_HEADS = ("•", "✓", "⚠")


def _app(tmp_path, config=None):
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M", **(config or {})}),
    )
    # Сборка executor не настоящая: McpProgress тест подаёт сам.
    app._executor_factory = lambda: object()
    return app


def _header(app):
    from gc_tui.widgets.start_header import StartHeader

    return app.screen.query_one("#brand-block", StartHeader)


def _lines(header) -> list[str]:
    """Строки шапки так, как их раскладывает сам виджет по своей ширине."""
    visual = header.render()
    return visual.laid_out(header.content_region.width).plain.splitlines()


def _status(header) -> str:
    return next(ln for ln in _lines(header) if ln.startswith(_STATUS_HEADS))


def _screen_rows(app) -> list[str]:
    return [
        "".join(seg.text for seg in strip).rstrip()
        for strip in app.screen._compositor.render_strips()
    ]


def _progress(statuses=None, outcome=None):
    from gc_tui.services.mcp_progress import McpProgress

    progress = McpProgress()
    progress.begin(["alpha", "beta"])
    for name, status in (statuses or {}).items():
        progress.update(name, {"status": status})
    if outcome is not None:
        progress.finish(outcome)
    return progress


_ALL_OK = {"alpha": "connected", "beta": "connected"}


def _spin(header, ticks: int = 40) -> None:
    for _ in range(ticks):
        header._tick()


async def _feed(app, pilot, progress) -> None:
    app._mcp_progress = progress
    app.screen._feed_start_header()
    await pilot.pause()


@pytest.mark.asyncio
async def test_mount_shows_loading_zero_of_m(tmp_path):
    app = _app(tmp_path, _TWO_SERVERS)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        header = _header(app)
        assert _status(header) == "• подключаю MCP 0/2"
        assert any("• подключаю MCP 0/2" in r for r in _screen_rows(app))
        assert any("Universal Agent" in r for r in _screen_rows(app))
        assert header._timer is not None


@pytest.mark.asyncio
async def test_no_servers_shows_ready_without_mcp_tail(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        header = _header(app)
        assert _status(header) == "✓ готов к работе"
        assert not any("MCP" in ln for ln in _lines(header))
        assert header._timer is None


@pytest.mark.asyncio
async def test_feed_then_settle_shows_ready(tmp_path):
    app = _app(tmp_path, _TWO_SERVERS)
    async with app.run_test(size=(120, 40)) as pilot:
        header = _header(app)
        await _feed(app, pilot, _progress(_ALL_OK, outcome=True))
        assert _status(header).startswith("• подключаю MCP")
        _spin(header)
        await pilot.pause()
        assert _status(header) == "✓ готов к работе   MCP 2/2 · /mcp"
        assert header._timer is None


@pytest.mark.asyncio
async def test_resume_and_notices_do_not_touch_loading_status(tmp_path):
    app = _app(tmp_path, _TWO_SERVERS)
    async with app.run_test(size=(120, 40)) as pilot:
        scr = app.screen
        header = _header(app)
        await _feed(app, pilot, _progress({"alpha": "connected"}))
        _spin(header, 3)
        app._update_available = True
        scr.on_screen_resume()
        app.refresh_notices_indicators()
        scr.refresh_update_notice()
        await pilot.pause()
        assert _status(header).startswith("• подключаю MCP")


@pytest.mark.asyncio
async def test_rebuild_restarts_loading(tmp_path):
    app = _app(tmp_path, _TWO_SERVERS)
    async with app.run_test(size=(120, 40)) as pilot:
        header = _header(app)
        await _feed(app, pilot, _progress(_ALL_OK, outcome=True))
        _spin(header)
        assert _status(header).startswith("✓ готов к работе")

        rebuilt = _progress()
        await _feed(app, pilot, rebuilt)
        assert _status(header) == "• подключаю MCP 0/2"
        assert header._timer is not None

        rebuilt.update("alpha", {"status": "connected"})
        rebuilt.update("beta", {"status": "failed"})
        rebuilt.finish(True)
        await _feed(app, pilot, rebuilt)
        _spin(header)
        await pilot.pause()
        assert _status(header) == "✓ готов к работе   ⚠ MCP 1/2 · /mcp"
        assert header._timer is None


@pytest.mark.asyncio
async def test_build_error_freezes_failed(tmp_path):
    app = _app(tmp_path, _TWO_SERVERS)
    async with app.run_test(size=(120, 40)) as pilot:
        header = _header(app)
        await _feed(app, pilot, _progress(outcome=False))
        assert _status(header) == "⚠ MCP: загрузка не завершилась · /mcp"
        assert header._timer is None
        _spin(header)
        assert _status(header) == "⚠ MCP: загрузка не завершилась · /mcp"


@pytest.mark.asyncio
async def test_update_tail_added_and_removed(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        scr = app.screen
        header = _header(app)
        await pilot.pause()
        assert not any("доступна новая версия" in ln for ln in _lines(header))

        app._update_available = True
        scr.refresh_update_notice()
        await pilot.pause()
        version_row = _lines(header)[1]
        assert "↑ доступна новая версия · /settings → Обновление" in (
            version_row
        )
        assert _status(header) == "✓ готов к работе"

        app._update_available = False
        scr.refresh_update_notice()
        await pilot.pause()
        assert not any("доступна новая версия" in ln for ln in _lines(header))


@pytest.mark.asyncio
async def test_settled_toasts_once(tmp_path, monkeypatch):
    from gc_core import startup_notices

    toasts = []
    monkeypatch.setattr(
        startup_notices,
        "warning_toasts",
        lambda notices: toasts.append(1) or ([], None, frozenset()),
    )
    app = _app(tmp_path, _TWO_SERVERS)
    async with app.run_test(size=(120, 40)) as pilot:
        scr = app.screen
        settled = []
        original = scr._maybe_toast_warnings
        monkeypatch.setattr(
            scr,
            "_maybe_toast_warnings",
            lambda: settled.append(1) or original(),
        )
        header = _header(app)
        await pilot.pause()
        assert settled == []  # загрузка ещё идёт — итога нет

        for _ in range(2):
            await _feed(app, pilot, _progress(_ALL_OK, outcome=True))
            _spin(header)
            await pilot.pause()
        assert len(settled) == 2  # по сигналу на поколение
        assert len(toasts) == 1  # а тосты — один раз за запуск


@pytest.mark.asyncio
async def test_settled_posted_after_mount_without_servers(
    tmp_path, monkeypatch
):
    """Без MCP-серверов итог наступает сразу — сигнал всё равно один,
    уже после монтирования (тосты предупреждений на старте)."""
    from gc_tui.screens.main import TraceScreen

    calls = []
    original = TraceScreen._maybe_toast_warnings

    def _count(self):
        calls.append(1)
        original(self)

    monkeypatch.setattr(TraceScreen, "_maybe_toast_warnings", _count)
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert len(calls) == 1


class _Ctx:
    os_backend = None
    tools_data: dict = {}
    mcp_status: dict = {}
    logging_path = "/tmp/agent_main_test.log"


class _Executor:
    def __init__(self, ctx):
        pass

    def stop_run(self):
        pass


def _real_build_app(tmp_path, monkeypatch, build):
    from gc_tui.app import TuiApp

    monkeypatch.setattr(
        "gc_core.config_modules.auth.build_auth_connector", lambda cfg: None
    )
    monkeypatch.setattr("gc_core.run_context.acreate_run_context", build)
    monkeypatch.setattr("gc_core.agent_executor.AgentExecutor", _Executor)
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(_TWO_SERVERS),
    )


def test_build_success_finishes_progress(tmp_path, monkeypatch):
    app = _real_build_app(
        tmp_path, monkeypatch, as_coro(lambda *a, **k: _Ctx())
    )
    app._build_real_executor()
    assert app._mcp_progress.outcome() is True


def test_build_error_marks_progress_failed(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("boom")

    app = _real_build_app(tmp_path, monkeypatch, as_coro(_boom))
    with pytest.raises(RuntimeError, match="boom"):
        app._build_real_executor()
    assert app._mcp_progress.outcome() is False
