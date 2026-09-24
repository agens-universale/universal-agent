"""MainScreen: batch waiting/active + drain nested subagent progress."""

from __future__ import annotations

import pytest
from gc_core.tools.subagents.progress import (
    QueueSubagentProgressSink,
    SubagentProgressEvent,
)
from gc_tui.widgets.tool_call_row import ToolCallRow


@pytest.mark.asyncio
async def test_batch_calls_first_active_rest_waiting():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.containers import VerticalScroll

    class _LiveRunner:
        def is_running(self) -> bool:
            return True

    app = TuiApp()
    app.active_runner = _LiveRunner()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = scr.query_one("#trace", VerticalScroll)
        a = ToolCallRow(
            call_id="a", tool_name="t1", args_summary="", waiting=True
        )
        b = ToolCallRow(
            call_id="b", tool_name="t2", args_summary="", waiting=True
        )
        c = ToolCallRow(
            call_id="c", tool_name="t3", args_summary="", waiting=True
        )
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
    from gc_tui.app import TuiApp
    from textual.containers import VerticalScroll

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


@pytest.mark.asyncio
async def test_nested_stays_after_parent_result():
    """После finish + result parent nested остаётся под ✓ (живая сессия)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.models import TraceEvent
    from textual.containers import VerticalScroll

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
                step=1,
                max_steps=50,
                elapsed_ms=0,
                tool_name="fs_read_file",
                tool_call_id="tc_nested",
                args_summary="path='a.py'",
            )
        )
        sink.emit(
            SubagentProgressEvent(
                parent_call_id="parent_1",
                child_run_id="child",
                kind="tool_end",
                step=1,
                max_steps=50,
                elapsed_ms=5,
                tool_name="fs_read_file",
                tool_call_id="tc_nested",
                ok=True,
            )
        )
        sink.emit(
            SubagentProgressEvent(
                parent_call_id="parent_1",
                child_run_id="child",
                kind="finished",
                step=1,
                max_steps=50,
                elapsed_ms=10,
                ok=True,
            )
        )
        scr._drain_subagent_progress()
        await pilot.pause()

        scr._render_event(
            TraceEvent(
                no=1,
                event_ts_utc_iso="",
                kind="result",
                title="",
                summary="ok · 1 step",
                full_payload={},
                call_id="parent_1",
            )
        )
        await pilot.pause()

        nested = scr._nested_tool_rows.get("tc_nested")
        assert nested is not None
        assert nested in scr._nested_widgets_by_parent.get("parent_1", [])
        assert "parent_1" not in scr._active_calls
        assert parent._result is not None
        assert "✓" in str(parent._render_prefix())
