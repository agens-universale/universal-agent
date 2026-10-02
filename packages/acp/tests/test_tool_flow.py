"""Хвост jsonl → карточка тула, без повтора уже записанного."""

import json

from gc_acp.tool_flow import ToolFlow, report_from_record


def _line(payload):
    return json.dumps(
        {"type": "message", "payload": payload}, ensure_ascii=False
    )


def test_poll_skips_bytes_already_on_disk_and_hides_bookkeeping(tmp_path):
    path = tmp_path / "data_acp_s.jsonl"
    path.write_text(
        _line(
            {
                "type": "function_call",
                "name": "fs_read_file",
                "call_id": "old",
                "arguments": '{"path": "old.py"}',
            }
        )
        + "\n",
        encoding="utf-8",
    )
    flow = ToolFlow(str(path))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            _line(
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "c1",
                    "arguments": '{"command": "pytest -q"}',
                }
            )
            + "\n"
        )
        handle.write(
            _line(
                {
                    "type": "function_call",
                    "name": "turn_ask_human",
                    "call_id": "ask",
                    "arguments": "{}",
                }
            )
            + "\n"
        )
        handle.write("not-json\n")
    updates = flow.poll()
    assert updates == [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "c1",
            "title": "exec_command(command='pytest -q')",
            "status": "in_progress",
        }
    ]
    assert flow.poll() == []


def test_poll_after_compaction_rewrites_shorter_file(tmp_path):
    path = tmp_path / "data_acp_s.jsonl"
    path.write_text(
        _line(
            {
                "type": "function_call",
                "name": "fs_read_file",
                "call_id": "before",
                "arguments": '{"path": "a.py"}',
            }
        )
        + "\n"
        + _line({"role": "assistant", "content": "длинный хвост " * 40})
        + "\n",
        encoding="utf-8",
    )
    flow = ToolFlow(str(path))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            _line(
                {
                    "type": "function_call",
                    "name": "fs_read_file",
                    "call_id": "mid",
                    "arguments": '{"path": "b.py"}',
                }
            )
            + "\n"
        )
    assert flow.poll()[0]["toolCallId"] == "mid"

    path.write_text(
        _line(
            {
                "role": "user",
                "content": '<technical type="dialog_summary">сводка</technical>',
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            _line(
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "after",
                    "arguments": '{"command": "ls"}',
                }
            )
            + "\n"
        )
    updates = flow.poll()
    assert updates == [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "after",
            "title": "exec_command(command='ls')",
            "status": "in_progress",
        }
    ]


def _sidecar(record):
    return json.dumps(record, ensure_ascii=False)


def _at(ts, payload):
    return json.dumps(
        {"ts": ts, "type": "message", "payload": payload},
        ensure_ascii=False,
    )


def test_poll_places_subagent_tools_between_call_and_result(tmp_path):
    thread = tmp_path / "data_acp_s.jsonl"
    sidecar = tmp_path / "data_subagents_acp_s.jsonl"
    thread.write_text("", encoding="utf-8")
    flow = ToolFlow(str(thread), sidecar_path=str(sidecar))
    thread.write_text(
        _at(
            "2026-01-01T00:00:01+00:00",
            {
                "type": "function_call",
                "name": "subagent_explore",
                "call_id": "sub",
                "arguments": '{"task": "найти"}',
            },
        )
        + "\n"
        + _at(
            "2026-01-01T00:00:03+00:00",
            {
                "type": "function_call_output",
                "call_id": "sub",
                "output": '{"ok": true}',
            },
        )
        + "\n",
        encoding="utf-8",
    )
    sidecar.write_text(
        _sidecar(
            {
                "ts": "2026-01-01T00:00:02+00:00",
                "seq": 1,
                "parent_call_id": "sub",
                "kind": "call",
                "payload": {
                    "type": "function_call",
                    "name": "fs_read_file",
                    "call_id": "c1",
                    "arguments": '{"path": "a.py"}',
                },
            }
        )
        + "\n"
        + _sidecar(
            {
                "ts": "2026-01-01T00:00:02.500000+00:00",
                "seq": 2,
                "parent_call_id": "sub",
                "kind": "result",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": '{"ok": true}',
                },
            }
        )
        + "\n"
        + _sidecar(
            {
                "ts": "2026-01-01T00:00:02.600000+00:00",
                "seq": 3,
                "parent_call_id": "sub",
                "kind": "call",
                "payload": {
                    "type": "function_call",
                    "name": "turn_finish",
                    "call_id": "fin",
                    "arguments": "{}",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert flow.poll() == [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "sub",
            "title": "subagent_explore(task='найти')",
            "status": "in_progress",
        },
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "sub:c1",
            "title": "fs_read_file(path='a.py')",
            "status": "in_progress",
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub:c1",
            "status": "completed",
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub",
            "status": "completed",
        },
    ]
    assert flow.poll() == []


def test_poll_reads_sidecar_created_after_start(tmp_path):
    thread = tmp_path / "data_acp_s.jsonl"
    sidecar = tmp_path / "data_subagents_acp_s.jsonl"
    flow = ToolFlow(str(thread), sidecar_path=str(sidecar))
    assert flow.poll() == []
    sidecar.write_text(
        _sidecar(
            {
                "seq": 1,
                "parent_call_id": "sub",
                "kind": "call",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "c1",
                    "arguments": '{"command": "ls"}',
                },
            }
        )
        + "\n"
        + _sidecar(
            {
                "seq": 2,
                "parent_call_id": "sub",
                "kind": "error",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": "boom",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert flow.poll() == [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "sub:c1",
            "title": "exec_command(command='ls')",
            "status": "in_progress",
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub:c1",
            "status": "failed",
        },
    ]


def test_report_without_call_id_is_skipped():
    seen: set[str] = set()
    assert (
        report_from_record(
            {
                "type": "function_call",
                "name": "fs_read_file",
                "arguments": "{}",
            },
            seen,
        )
        is None
    )
    assert seen == set()
