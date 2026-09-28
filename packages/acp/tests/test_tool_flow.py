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
