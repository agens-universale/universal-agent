"""Шаг 6: Ctrl+click nested → ToolCallDetailScreen из sidecar на диске."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gc_core.thread_store import append_subagent_trace_record
from gc_core.tools.subagents.sidecar import apersist_subagent_trace
from gc_tui.services.subagent_sidecar_mapper import (
    detail_from_sidecar_records,
    resolve_nested_tool_detail,
    trace_events_from_nested_detail,
)


def test_detail_from_sidecar_records_pairs_call_result():
    records = [
        {
            "seq": 1,
            "kind": "call",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": '{"cmd":"ls"}',
                "call_id": "c9",
            },
        },
        {
            "seq": 2,
            "kind": "result",
            "payload": {
                "type": "function_call_output",
                "call_id": "c9",
                "output": "a.txt\n",
            },
        },
    ]
    detail = detail_from_sidecar_records(records, "c9")
    assert detail is not None
    assert detail["call"]["name"] == "shell"
    assert detail["result"]["output"] == "a.txt\n"
    call_ev, result_ev = trace_events_from_nested_detail("c9", detail)
    assert call_ev.tool_name == "shell"
    assert "ls" in call_ev.summary
    assert result_ev is not None
    assert result_ev.full_payload["output"] == "a.txt\n"


def test_resolve_uses_sidecar_records():
    records = [
        {
            "seq": 1,
            "kind": "call",
            "payload": {
                "type": "function_call",
                "name": "fs_read_file",
                "arguments": '{"path":"disk.py"}',
                "call_id": "c_disk",
            },
        },
        {
            "seq": 2,
            "kind": "result",
            "payload": {
                "type": "function_call_output",
                "call_id": "c_disk",
                "output": "from-disk",
            },
        },
    ]
    detail = resolve_nested_tool_detail("c_disk", sidecar_records=records)
    assert detail is not None
    assert "disk.py" in detail["call"]["arguments"]
    assert detail["result"]["output"] == "from-disk"
    assert resolve_nested_tool_detail("c_disk", sidecar_records=None) is None
    assert (
        resolve_nested_tool_detail("missing", sidecar_records=records) is None
    )


def _write_parent_and_sidecar(tmp_path: Path, tid: str) -> None:
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "status": "stopped",
                "updated_at": "2026-07-18T12:00:00+00:00",
                "messages_count": 3,
            }
        ),
        encoding="utf-8",
    )
    lines = [
        {
            "type": "message",
            "seq": 1,
            "ts": "2026-07-18T12:00:00+00:00",
            "payload": {"id": "u1", "role": "user", "content": "go"},
        },
        {
            "type": "message",
            "seq": 2,
            "ts": "2026-07-18T12:00:01+00:00",
            "payload": {
                "id": "fc1",
                "type": "function_call",
                "name": "subagent_worker",
                "arguments": '{"task": "x", "reason": "y"}',
                "call_id": "call_sub_1",
            },
        },
        {
            "type": "message",
            "seq": 3,
            "ts": "2026-07-18T12:00:05+00:00",
            "payload": {
                "id": "fco1",
                "type": "function_call_output",
                "call_id": "call_sub_1",
                "output": '{"ok": true}',
            },
        },
    ]
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
        encoding="utf-8",
    )
    d = str(tmp_path)
    append_subagent_trace_record(
        d,
        "cli",
        tid,
        parent_call_id="call_sub_1",
        child_run_id="run_c1",
        kind="call",
        payload={
            "type": "function_call",
            "name": "fs_read_file",
            "arguments": '{"path": "nested.py"}',
            "call_id": "call_child_9",
        },
    )
    append_subagent_trace_record(
        d,
        "cli",
        tid,
        parent_call_id="call_sub_1",
        child_run_id="run_c1",
        kind="result",
        payload={
            "type": "function_call_output",
            "call_id": "call_child_9",
            "output": json.dumps({"ok": True, "content": "print(1)"}),
        },
    )


@pytest.mark.asyncio
async def test_ctrl_click_nested_opens_detail_from_sidecar(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
    from textual import events

    tid = "nested_detail"
    _write_parent_and_sidecar(tmp_path, tid)

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(tid, is_resume=True) is True
        await pilot.pause(0.3)

        nested = scr._nested_tool_rows.get("call_child_9")
        assert nested is not None
        # nested отсутствует в parent _task_events
        assert not any(
            ev.call_id == "call_child_9" and ev.kind == "call"
            for ev in scr._task_events
        )

        click = events.Click(
            widget=nested,
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
        nested.on_click(click)
        await pilot.pause(0.2)

        assert isinstance(pilot.app.screen, ToolCallDetailScreen)
        header = str(pilot.app.screen.query_one("#header").render())
        assert "fs_read_file" in header
        body = "\n".join(
            str(s.render()) for s in pilot.app.screen.query("Static")
        )
        assert "nested.py" in body or "path" in body
        assert "print(1)" in body


@pytest.mark.asyncio
async def test_ctrl_click_nested_from_persisted_sidecar(tmp_path):
    """Mid-session: detail из sidecar на диске (persist), не из in-memory cache."""
    pytest.importorskip("textual")
    from types import SimpleNamespace

    from gc_tui.app import TuiApp
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual import events
    from textual.containers import VerticalScroll

    threads = str(tmp_path)
    ctx = SimpleNamespace(
        parent_call_id="call_sub_1",
        run_id="run_c1",
        threads_dir=threads,
        system_id="cli",
        thread_id="live_disk",
    )
    await apersist_subagent_trace(
        ctx,
        kind="call",
        payload={
            "type": "function_call",
            "name": "shell",
            "arguments": '{"cmd":"pwd"}',
            "call_id": "live_child",
        },
    )
    await apersist_subagent_trace(
        ctx,
        kind="result",
        payload={
            "type": "function_call_output",
            "call_id": "live_child",
            "output": "/tmp\n",
        },
    )

    app = TuiApp(threads_dir=threads, system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "live_disk"
        from gc_tui.services.stores.file_store import FileTaskStore

        scr._store = FileTaskStore(threads_dir=threads, system="cli")
        trace = scr.query_one("#trace", VerticalScroll)
        parent = ToolCallRow(
            call_id="call_sub_1",
            tool_name="subagent_worker",
            args_summary="task=x",
        )
        nested = ToolCallRow(
            call_id="live_child",
            tool_name="shell",
            args_summary="cmd='pwd'",
            nested=True,
        )
        trace.mount(parent)
        scr._subagent_parent_rows["call_sub_1"] = parent
        trace.mount(nested)
        scr._nested_tool_rows["live_child"] = nested
        await pilot.pause()

        click = events.Click(
            widget=nested,
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
        nested.on_click(click)
        await pilot.pause(0.2)

        assert isinstance(pilot.app.screen, ToolCallDetailScreen)
        body = "\n".join(
            str(s.render()) for s in pilot.app.screen.query("Static")
        )
        assert "pwd" in body
        assert "/tmp" in body
