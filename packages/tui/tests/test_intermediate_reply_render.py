"""Рендер intermediate_reply (· проза) и error_msg ([!] красный) в трейсе."""

from gc_testkit.tui_support import tui_settings
import pytest
from gc_tui.models import TraceEvent


@pytest.mark.asyncio
async def test_intermediate_reply_renders_as_dot_prose(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        ev = TraceEvent(
            no=1,
            event_ts_utc_iso="t",
            kind="agent",
            title="",
            summary="нашёл проблему, чиню...",
            full_payload={},
            tool_name="intermediate_reply",
        )
        screen._render_event(ev)
        await pilot.pause()
        rows = [
            w
            for w in screen.query(TraceRow)
            if w.has_class("line-intermediate")
        ]
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_error_msg_renders_red_bang(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        ev = TraceEvent(
            no=1,
            event_ts_utc_iso="t",
            kind="error_msg",
            title="!",
            summary="HTTP 500: backend unavailable.",
            full_payload={},
        )
        screen._render_event(ev)
        await pilot.pause()
        rows = [w for w in screen.query(TraceRow) if w.has_class("line-error")]
        assert len(rows) == 1
