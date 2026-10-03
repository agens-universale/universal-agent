"""/mcp показывает результат boot-дискавери сразу (warm-on-start выставляет
app._mcp_snapshot), а не ждёт первого сообщения."""

from gc_testkit.tui_support import tui_settings
import pytest
from gc_testkit import as_coro


@pytest.mark.asyncio
async def test_mcp_screen_prefers_boot_snapshot(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        boot = {
            "office": {
                "status": "connected",
                "reason": None,
                "description": "d",
                "tools": [],
            }
        }
        app._mcp_snapshot = boot
        assert scr._mcp_screen_snapshot() == boot  # предпочитаем boot-снапшот


@pytest.mark.asyncio
async def test_mcp_screen_snapshot_none_without_boot_or_task(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app._mcp_snapshot = None
        scr._active_task_id = None
        assert scr._mcp_screen_snapshot() is None


def test_build_real_executor_stores_mcp_snapshot(tmp_path, monkeypatch):
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "mcp-servers": {
                    "office": {
                        "transport": "stdio",
                        "command": "x",
                        "description": "Doc reader",
                    }
                }
            }
        ),
    )

    class _Ctx:
        # У настоящего RunContext бэкенд есть всегда; двойнику он нужен, потому что
        # TuiApp включает diff-журнал сам (attach_to_context) — раньше это делало
        # ядро внутри acreate_run_context, то есть внутри подменённой функции.
        os_backend = None
        tools_data = {"mcp_office_read": {"description": "read"}}
        mcp_status = {
            "office": {
                "status": "connected",
                "reason": None,
                "tool_count": 1,
                "elapsed_ms": 1,
            }
        }
        logging_path = "/tmp/agent_main_test.log"

    class _Ex:
        def __init__(self, ctx):
            pass

        def stop_run(self):
            pass

    monkeypatch.setattr(
        "gc_core.config.build_auth_connector", lambda cfg: None
    )
    monkeypatch.setattr(
        "gc_core.run_context.acreate_run_context",
        as_coro(lambda *a, **k: _Ctx()),
    )
    monkeypatch.setattr("gc_core.agent_executor.AgentExecutor", _Ex)
    app._build_real_executor()
    assert app._mcp_snapshot is not None
    assert app._mcp_snapshot["office"]["status"] == "connected"
    assert app._mcp_snapshot["office"]["description"] == "Doc reader"
