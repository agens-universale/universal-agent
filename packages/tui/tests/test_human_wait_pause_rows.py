"""Человеко-ожидание ставит на паузу и вложенные строки субагента."""

import pytest
from gc_testkit.tui_support import tui_settings
from gc_tui.widgets.tool_call_row import ToolCallRow


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


@pytest.mark.asyncio
async def test_human_wait_pauses_nested_rows(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        root = ToolCallRow(
            call_id="r", tool_name="subagent_x", args_summary=""
        )
        nested = ToolCallRow(
            call_id="n", tool_name="exec_run", args_summary="", nested=True
        )
        await scr.mount(root)
        await scr.mount(nested)
        # Дальше без await: живой тик опроса без раннера чистит строки.
        scr._active_calls["r"] = root
        scr._nested_tool_rows["n"] = nested
        scr._apply_human_wait(True)
        assert nested._pulse_timer is None
        assert "‖" in nested._prefix_static.content.plain
        assert root._pulse_timer is None

        scr._apply_human_wait(False)
        assert nested._pulse_timer is not None
        assert "▸" in nested._prefix_static.content.plain
        assert root._pulse_timer is not None
