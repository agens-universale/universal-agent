"""Карточка тула из хвоста jsonl: без тел, без служебных тулов."""

import json

from gc_responses.tool_flow import ToolFlow


def _write(path, payloads: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        for payload in payloads:
            record = {"type": "message", "payload": payload}
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def test_card_has_title_and_no_bodies(tmp_path):
    path = tmp_path / "thread.jsonl"
    flow = ToolFlow(str(path))
    _write(
        path,
        [
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "c1",
                "arguments": json.dumps({"path": "src/app.py"}),
            }
        ],
    )
    opened = flow.poll()
    assert len(opened) == 1
    card = opened[0]
    assert card["op"] == "open"
    assert card["name"] == "read_file"
    assert card["title"].startswith("read_file(")
    assert "path" in card["title"]
    assert "arguments" not in card
    assert "output" not in card
    _write(
        path,
        [
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": json.dumps(
                    {"ok": False, "error": {"message": "сырое тело"}}
                ),
            }
        ],
    )
    closed = flow.poll()
    assert closed == [{"op": "close", "call_id": "c1", "status": "failed"}]
    assert "сырое тело" not in json.dumps(closed)


def test_success_output_completes_and_hides_body(tmp_path):
    path = tmp_path / "thread.jsonl"
    flow = ToolFlow(str(path))
    _write(
        path,
        [
            {
                "type": "function_call",
                "name": "list_dir",
                "call_id": "c2",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "c2",
                "output": json.dumps({"ok": True, "files": ["a.py"]}),
            },
        ],
    )
    reports = flow.poll()
    assert reports[0]["title"] == "list_dir"
    assert reports[1]["status"] == "completed"
    assert "a.py" not in json.dumps(reports)


def test_hidden_tools_and_duplicate_call_id(tmp_path):
    path = tmp_path / "thread.jsonl"
    flow = ToolFlow(str(path))
    _write(
        path,
        [
            {
                "type": "function_call",
                "name": "turn_finish",
                "call_id": "f",
                "arguments": "{}",
            },
            {
                "type": "function_call",
                "name": "turn_ask_human",
                "call_id": "h",
                "arguments": "{}",
            },
            {
                "type": "function_call",
                "name": "turn_intermediate_reply",
                "call_id": "i",
                "arguments": "{}",
            },
            {
                "type": "function_call",
                "name": "session_think",
                "call_id": "s",
                "arguments": "{}",
            },
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "same",
                "arguments": "{}",
            },
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "same",
                "arguments": json.dumps({"path": "second"}),
            },
        ],
    )
    reports = flow.poll()
    assert [item["call_id"] for item in reports] == ["same"]
    blob = json.dumps(reports)
    assert "turn_finish" not in blob
    assert "turn_ask_human" not in blob
    assert "turn_intermediate_reply" not in blob
    assert "session_think" not in blob


def test_offset_skips_lines_from_before_this_turn(tmp_path):
    path = tmp_path / "thread.jsonl"
    _write(
        path,
        [
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "old",
                "arguments": "{}",
            }
        ],
    )
    flow = ToolFlow(str(path))
    _write(
        path,
        [
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "new",
                "arguments": "{}",
            }
        ],
    )
    reports = flow.poll()
    assert [item["call_id"] for item in reports] == ["new"]


def test_poll_after_compaction_reads_rewritten_shorter_file(tmp_path):
    path = tmp_path / "thread.jsonl"
    _write(
        path,
        [
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "before",
                "arguments": "{}",
            },
            {"role": "assistant", "content": "длинный хвост " * 40},
        ],
    )
    flow = ToolFlow(str(path))
    _write(
        path,
        [
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "mid",
                "arguments": json.dumps({"path": "b.py"}),
            }
        ],
    )
    assert flow.poll()[0]["call_id"] == "mid"

    path.write_text(
        json.dumps(
            {
                "type": "message",
                "payload": {
                    "role": "user",
                    "content": (
                        '<technical type="dialog_summary">сводка</technical>'
                    ),
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    _write(
        path,
        [
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "after",
                "arguments": json.dumps({"command": "ls"}),
            }
        ],
    )
    reports = flow.poll()
    assert [item["call_id"] for item in reports] == ["after"]
    assert reports[0]["op"] == "open"
    assert reports[0]["title"] == "exec_command(command='ls')"
    assert "сводка" not in json.dumps(reports)
