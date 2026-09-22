import pytest


@pytest.mark.asyncio
async def test_on_mount_triggers_warm(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    calls = {"n": 0}

    class _Ex:
        def stop_run(self):
            pass

    app._executor_factory = lambda: calls.__setitem__("n", calls["n"] + 1) or _Ex()
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert calls["n"] == 1


@pytest.mark.asyncio
async def test_poll_renders_indicator_without_active_task(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.mcp_progress import McpProgress

    class _Ex:
        def stop_run(self):
            pass

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Ex()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = None
        prog = McpProgress()
        prog.begin(["computer"])
        app._mcp_progress = prog
        scr._poll_active()
        await pilot.pause()
        assert scr._mcp_status_row is not None
