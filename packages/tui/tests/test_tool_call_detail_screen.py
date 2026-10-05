"""ToolCallDetailScreen — CALL + OUTPUT секции, JSON-подсветка / plain fallback."""

from gc_testkit.tui_support import tui_settings
import json
from pathlib import Path

import pytest


def _write_thread_with_tool_call(tmp_path, tid, call_payload, result_payload):
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "status": "stopped",
                "updated_at": "2026-05-27T12:00:00+00:00",
                "messages_count": 3,
            }
        ),
        encoding="utf-8",
    )
    lines = [
        {
            "type": "message",
            "seq": 1,
            "ts_utc": "2026-05-27T12:00:00+00:00",
            "payload": {"id": "u1", "role": "user", "content": "запрос"},
        },
        {
            "type": "message",
            "seq": 2,
            "ts_utc": "2026-05-27T12:00:01+00:00",
            "payload": call_payload,
        },
    ]
    if result_payload is not None:
        lines.append(
            {
                "type": "message",
                "seq": 3,
                "ts_utc": "2026-05-27T12:00:02+00:00",
                "payload": result_payload,
            }
        )
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_ctrl_click_opens_detail_screen_with_call_and_output_sections(
    tmp_path,
):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual import events

    tid = "detail_test"
    _write_thread_with_tool_call(
        tmp_path,
        tid,
        call_payload={
            "type": "function_call",
            "name": "read_file",
            "arguments": {"path": "x.py"},
            "call_id": "c1",
        },
        result_payload={
            "type": "function_call_output",
            "call_id": "c1",
            "output": json.dumps({"ok": True, "content": "x = 1\ny = 2"}),
        },
    )

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        row = pilot.app.screen.query_one(ToolCallRow)
        click = events.Click(
            widget=row,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=True,
            chain=1,
        )
        row.on_click(click)
        await pilot.pause(0.2)

        assert isinstance(pilot.app.screen, ToolCallDetailScreen)
        # Обе section-header'а присутствуют
        headers = list(pilot.app.screen.query(".detail-section-header"))
        assert len(headers) == 2
        # Header экрана содержит имя тула
        header_text = str(pilot.app.screen.query_one("#header").render())
        assert "read_file" in header_text


@pytest.mark.asyncio
async def test_detail_screen_pending_row_shows_placeholder(tmp_path):
    """Если result ещё не пришёл — OUTPUT-секция показывает placeholder."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual import events
    from textual.widgets import Static

    tid = "detail_pending"
    _write_thread_with_tool_call(
        tmp_path,
        tid,
        call_payload={
            "type": "function_call",
            "name": "read_file",
            "arguments": {"path": "x.py"},
            "call_id": "c1",
        },
        result_payload=None,  # pending
    )

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        row = pilot.app.screen.query_one(ToolCallRow)
        click = events.Click(
            widget=row,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=True,
            chain=1,
        )
        row.on_click(click)
        await pilot.pause(0.2)

        assert isinstance(pilot.app.screen, ToolCallDetailScreen)
        body_text = "\n".join(
            str(s.render()) for s in pilot.app.screen.query(Static)
        )
        assert "результат ещё не получен" in body_text


@pytest.mark.asyncio
async def test_detail_screen_plain_output_not_json(tmp_path):
    """Если output — не JSON (raw stdout): рендерится plain без подсветки, без падения."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual import events
    from textual.widgets import Static

    tid = "detail_plain"
    _write_thread_with_tool_call(
        tmp_path,
        tid,
        call_payload={
            "type": "function_call",
            "name": "execute_command",
            "arguments": {"command": "ls"},
            "call_id": "c1",
        },
        result_payload={
            "type": "function_call_output",
            "call_id": "c1",
            "output": "Traceback (most recent call last):\n  File ...\nRuntimeError",
        },
    )

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        row = pilot.app.screen.query_one(ToolCallRow)
        click = events.Click(
            widget=row,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=True,
            chain=1,
        )
        row.on_click(click)
        await pilot.pause(0.2)

        assert isinstance(pilot.app.screen, ToolCallDetailScreen)
        body_text = "\n".join(
            str(s.render()) for s in pilot.app.screen.query(Static)
        )
        assert "Traceback" in body_text
        assert "RuntimeError" in body_text


@pytest.mark.asyncio
async def test_detail_screen_payload_is_selectable(tmp_path):
    """Экран события: get_selection на каждом Static payload-а возвращает текст.

    Это контракт Ctrl+C: app._current_selection -> screen.get_selected_text
    собирает текст только с виджетов, чей get_selection не None.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual import events
    from textual.selection import Selection
    from textual.widgets import Static

    tid = "detail_selectable"
    _write_thread_with_tool_call(
        tmp_path,
        tid,
        call_payload={
            "type": "function_call",
            "name": "read_file",
            "arguments": {"path": "x.py"},
            "call_id": "c1",
        },
        result_payload={
            "type": "function_call_output",
            "call_id": "c1",
            "output": json.dumps({"ok": True, "content": "x = 1\ny = 2"}),
        },
    )

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        row = pilot.app.screen.query_one(ToolCallRow)
        click = events.Click(
            widget=row,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=True,
            chain=1,
        )
        row.on_click(click)
        await pilot.pause(0.2)

        assert isinstance(pilot.app.screen, ToolCallDetailScreen)
        statics = pilot.app.screen.query("PayloadBlocks > Static").results(
            Static
        )
        texts = [w.get_selection(Selection(None, None)) for w in statics]
        assert texts and all(t is not None for t in texts)
        extracted = "".join(t[0] for t in texts)
        assert extracted.strip()
