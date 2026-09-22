"""Индикатор живёт в фиксированной #indicator-bar (вне скролла #trace) —
растущий стрим не может на него наложиться."""
import json
from pathlib import Path

import pytest


def _write_task(tmp_path, tid):
    meta = {"thread_id": tid, "status": "running",
            "updated_at": "2026-06-15T12:00:00+00:00", "messages_count": 0}
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(json.dumps(meta), encoding="utf-8")
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


@pytest.mark.asyncio
async def test_indicator_docked_not_in_trace(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator
    from textual.containers import Vertical, VerticalScroll

    tid = "dock1"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._show_thinking_indicator(state="thinking")
        await pilot.pause()
        bar = pilot.app.query_one("#indicator-bar", Vertical)
        trace = pilot.app.query_one("#trace", VerticalScroll)
        assert any(isinstance(w, ThinkingIndicator) for w in bar.children)
        assert not any(isinstance(w, ThinkingIndicator) for w in trace.children)


@pytest.mark.asyncio
async def test_trace_row_lands_in_trace_with_indicator_docked(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator
    from gc_tui.widgets.trace_row import TraceRow
    from textual.containers import VerticalScroll

    tid = "dock2"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._show_thinking_indicator(state="thinking")
        scr._render_record({"payload": {"id": "a1", "role": "assistant",
                                        "content": "привет"}}, live=True)
        await pilot.pause()
        trace = pilot.app.query_one("#trace", VerticalScroll)
        assert any(isinstance(w, TraceRow) for w in trace.children)        # строка в трейсе
        assert not any(isinstance(w, ThinkingIndicator) for w in trace.children)  # индикатор — нет
