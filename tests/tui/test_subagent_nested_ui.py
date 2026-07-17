"""MainScreen: batch waiting/active + drain nested subagent progress."""
from __future__ import annotations

import pytest

from tools.subagents.progress import QueueSubagentProgressSink, SubagentProgressEvent
from tui.widgets.tool_call_row import ToolCallRow


@pytest.mark.asyncio
async def test_batch_calls_first_active_rest_waiting():
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp

    class _LiveRunner:
        def is_running(self) -> bool:
            return True

    app = TuiApp()
    app.active_runner = _LiveRunner()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = scr.query_one("#trace", VerticalScroll)
        a = ToolCallRow(call_id="a", tool_name="t1", args_summary="", waiting=True)
        b = ToolCallRow(call_id="b", tool_name="t2", args_summary="", waiting=True)
        c = ToolCallRow(call_id="c", tool_name="t3", args_summary="", waiting=True)
        for row in (a, b, c):
            trace.mount(row)
            scr._active_calls[row.call_id] = row
        await pilot.pause()
        scr._reconcile_active_tool_phases()
        await pilot.pause()
        assert "▸" in str(a._render_prefix())
        assert "·" in str(b._render_prefix())
        assert "прервано" not in str(b._render_content())
        assert "·" in str(c._render_prefix())


@pytest.mark.asyncio
async def test_drain_mounts_nested_under_parent():
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp

    class _LiveRunner:
        def is_running(self) -> bool:
            return True

    app = TuiApp()
    app.active_runner = _LiveRunner()
    sink = QueueSubagentProgressSink()
    app._subagent_progress_sink = sink

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = scr.query_one("#trace", VerticalScroll)
        parent = ToolCallRow(
            call_id="parent_1",
            tool_name="subagent_worker",
            args_summary="task=x",
        )
        trace.mount(parent)
        scr._active_calls["parent_1"] = parent
        await pilot.pause()

        sink.emit(
            SubagentProgressEvent(
                parent_call_id="parent_1",
                child_run_id="child",
                kind="tool_start",
                step=2,
                max_steps=50,
                elapsed_ms=0,
                tool_name="fs_read_file",
                tool_call_id="tc_nested",
                args_summary="path='a.py'",
            )
        )
        scr._drain_subagent_progress()
        await pilot.pause()

        nested = scr._nested_tool_rows.get("tc_nested")
        assert nested is not None
        assert nested._nested is True
        assert "fs_read_file" in str(nested._render_content())
        # heartbeat на корне
        assert "step 2/50" in str(parent._render_content())
