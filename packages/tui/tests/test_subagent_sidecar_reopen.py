"""Reopen: nested из data_subagents_* под subagent_* (в т.ч. после result)."""

from __future__ import annotations

from gc_testkit.tui_support import tui_settings
import json
from pathlib import Path

import pytest
from gc_core.thread_store import append_subagent_trace_record
from gc_tui.services.subagent_sidecar_mapper import CHILD_SUMMARY_LABEL
from gc_tui.widgets.tool_call_row import ToolCallRow
from gc_tui.widgets.trace_row import TraceRow


def _write_parent_thread(tmp_path: Path, tid: str) -> None:
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
            "payload": {"id": "u1", "role": "user", "content": "делегируй"},
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
                "output": '{"ok": true, "result": "done"}',
            },
        },
    ]
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
        encoding="utf-8",
    )


def _write_sidecar(tmp_path: Path, tid: str) -> None:
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
            "arguments": '{"path": "a.py"}',
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
            "output": "ok",
        },
    )
    append_subagent_trace_record(
        d,
        "cli",
        tid,
        parent_call_id="call_sub_1",
        child_run_id="run_c1",
        kind="child_summary",
        payload={"synthesis": "сжато"},
    )
    append_subagent_trace_record(
        d,
        "cli",
        tid,
        parent_call_id="call_sub_1",
        child_run_id="run_c1",
        kind="intermediate_reply",
        payload={"prose": "проверю дальше", "call_id": "ir1"},
    )
    append_subagent_trace_record(
        d,
        "cli",
        tid,
        parent_call_id="call_sub_1",
        child_run_id="run_c1",
        kind="finished",
        payload={"ok": True, "finish_reason": "completed", "steps": 2},
    )


def _sum_msg(text: str) -> dict:
    return {
        "role": "user",
        "id": "m_s",
        "content": (
            f'<technical type="dialog_summary">Summarized context.\n<summary>{text}</summary></technical>'
        ),
    }


def _write_archived_subagent_thread(tmp_path: Path, tid: str) -> None:
    """subagent_* в __pre_summary__; live начинается с dialog_summary."""
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "status": "stopped",
                "updated_at": "2026-07-18T12:00:00+00:00",
                "messages_count": 4,
                "last_summary_archive_suffix": "20260718_120000",
            }
        ),
        encoding="utf-8",
    )
    archive = [
        {
            "type": "message",
            "seq": 1,
            "ts": "2026-07-18T11:00:00+00:00",
            "payload": {"id": "u1", "role": "user", "content": "делегируй"},
        },
        {
            "type": "message",
            "seq": 2,
            "ts": "2026-07-18T11:00:01+00:00",
            "payload": {
                "id": "fc1",
                "type": "function_call",
                "name": "subagent_worker",
                "arguments": '{"task": "x", "reason": "y"}',
                "call_id": "call_sub_arch",
            },
        },
        {
            "type": "message",
            "seq": 3,
            "ts": "2026-07-18T11:00:05+00:00",
            "payload": {
                "id": "fco1",
                "type": "function_call_output",
                "call_id": "call_sub_arch",
                "output": '{"ok": true}',
            },
        },
    ]
    (
        tmp_path / f"data_cli_{tid}__pre_summary__20260718_120000.jsonl"
    ).write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in archive) + "\n",
        encoding="utf-8",
    )
    live = [
        {
            "type": "message",
            "seq": 1,
            "ts": "2026-07-18T12:00:00+00:00",
            "payload": _sum_msg("после делегации"),
        },
        {
            "type": "message",
            "seq": 2,
            "ts": "2026-07-18T12:00:01+00:00",
            "payload": {"id": "u2", "role": "user", "content": "дальше"},
        },
    ]
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in live) + "\n",
        encoding="utf-8",
    )
    append_subagent_trace_record(
        str(tmp_path),
        "cli",
        tid,
        parent_call_id="call_sub_arch",
        child_run_id="run_arch",
        kind="call",
        payload={
            "type": "function_call",
            "name": "fs_list_files",
            "arguments": '{"path": "."}',
            "call_id": "child_arch_1",
        },
    )
    append_subagent_trace_record(
        str(tmp_path),
        "cli",
        tid,
        parent_call_id="call_sub_arch",
        child_run_id="run_arch",
        kind="result",
        payload={
            "type": "function_call_output",
            "call_id": "child_arch_1",
            "output": "ok",
        },
    )


@pytest.mark.asyncio
async def test_reopen_merges_nested_under_archived_subagent(tmp_path):
    """Приёмка #4: nested стыкуется к archived subagent_* в __pre_summary__."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "reopen_arch"
    _write_archived_subagent_thread(tmp_path, tid)

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(tid, is_resume=True) is True
        await pilot.pause(0.3)

        assert "call_sub_arch" in scr._subagent_parent_rows
        nested = scr._nested_tool_rows.get("child_arch_1")
        assert nested is not None
        assert nested._nested is True
        assert "fs_list_files" in str(nested._render_content())


@pytest.mark.asyncio
async def test_reopen_merges_nested_under_finished_subagent(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "reopen_sub"
    _write_parent_thread(tmp_path, tid)
    _write_sidecar(tmp_path, tid)

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(tid, is_resume=True) is True
        await pilot.pause(0.3)

        # parent уже завершён — нет в _active_calls, но nested смонтированы
        assert "call_sub_1" not in scr._active_calls
        assert "call_sub_1" in scr._subagent_parent_rows

        nested = scr._nested_tool_rows.get("call_child_9")
        assert nested is not None
        assert nested._nested is True
        assert "fs_read_file" in str(nested._render_content())
        assert nested._result is not None

        widgets = scr._nested_widgets_by_parent.get("call_sub_1", [])
        prose_texts = [
            getattr(w, "_content", "")
            for w in widgets
            if isinstance(w, TraceRow)
        ]
        assert CHILD_SUMMARY_LABEL in prose_texts
        assert "проверю дальше" in prose_texts
        assert any(isinstance(w, ToolCallRow) and w._nested for w in widgets)
