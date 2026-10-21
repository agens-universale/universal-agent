"""Карточка тула из хвоста jsonl: без тел, без служебных тулов."""

import json

from gc_chat.responses.tool_flow import ToolFlow


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
    assert card["title"] == "read_file(src/app.py)"
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
    assert reports[0]["title"] == "exec_command(ls)"
    assert "сводка" not in json.dumps(reports)


def _sidecar(path, records: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _nested_call(parent: str, call_id: str, name: str, arguments: str) -> dict:
    return {
        "seq": 1,
        "parent_call_id": parent,
        "child_run_id": "child",
        "kind": "call",
        "payload": {
            "type": "function_call",
            "name": name,
            "call_id": call_id,
            "arguments": arguments,
        },
    }


def test_sidecar_tool_is_a_card_under_the_parent(tmp_path):
    thread = tmp_path / "data_responses_t.jsonl"
    thread.write_text("", encoding="utf-8")
    flow = ToolFlow(str(thread))
    sidecar = tmp_path / "data_subagents_responses_t.jsonl"
    _sidecar(
        sidecar,
        [
            _nested_call(
                "sub",
                "c1",
                "read_file",
                json.dumps({"path": "src/app.py"}),
            ),
            {
                "parent_call_id": "sub",
                "kind": "user",
                "payload": {"role": "user", "content": "служебное"},
            },
            {
                "parent_call_id": "sub",
                "kind": "intermediate_reply",
                "payload": {"prose": "промежуточная проза"},
            },
            {
                "parent_call_id": "sub",
                "kind": "child_summary",
                "payload": {"synthesis": "сводка"},
            },
            _nested_call("sub", "hide", "turn_finish", "{}"),
            {
                "parent_call_id": "sub",
                "kind": "result",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": json.dumps({"ok": True, "files": ["a.py"]}),
                },
            },
        ],
    )
    reports = flow.poll()
    assert [item["op"] for item in reports] == ["open", "close"]
    opened, closed = reports
    assert opened["call_id"] == "sub/c1"
    assert opened["parent_call_id"] == "sub"
    assert opened["name"] == "read_file"
    assert opened["title"].startswith("read_file(")
    assert closed == {
        "op": "close",
        "call_id": "sub/c1",
        "status": "completed",
    }
    blob = json.dumps(reports, ensure_ascii=False)
    assert "src/app.py" in opened["title"]
    assert "arguments" not in opened
    assert "a.py" not in blob
    assert "служебное" not in blob
    assert "промежуточная" not in blob
    assert "turn_finish" not in blob


def test_same_child_call_id_under_two_parents_are_two_cards(tmp_path):
    thread = tmp_path / "thread.jsonl"
    flow = ToolFlow(str(thread))
    sidecar = tmp_path / "thread.subagents.jsonl"
    _sidecar(
        sidecar,
        [
            _nested_call("left", "c1", "read_file", "{}"),
            _nested_call("right", "c1", "exec_command", "{}"),
        ],
    )
    reports = flow.poll()
    assert [item["call_id"] for item in reports] == ["left/c1", "right/c1"]
    assert [item["parent_call_id"] for item in reports] == ["left", "right"]


def test_sidecar_error_closes_the_card_as_failed(tmp_path):
    thread = tmp_path / "thread.jsonl"
    flow = ToolFlow(str(thread))
    sidecar = tmp_path / "thread.subagents.jsonl"
    _sidecar(sidecar, [_nested_call("sub", "c1", "exec_command", "{}")])
    assert flow.poll()[0]["op"] == "open"
    _sidecar(
        sidecar,
        [
            {
                "parent_call_id": "sub",
                "kind": "error",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": "не json",
                },
            }
        ],
    )
    assert flow.poll() == [
        {"op": "close", "call_id": "sub/c1", "status": "failed"}
    ]


def test_sidecar_after_rewrite_still_reports_the_new_call(tmp_path):
    thread = tmp_path / "thread.jsonl"
    flow = ToolFlow(str(thread))
    sidecar = tmp_path / "thread.subagents.jsonl"
    _sidecar(
        sidecar,
        [_nested_call("sub", "before", "read_file", "{}")]
        + [{"role": "pad", "content": "длинный хвост " * 40}],
    )
    assert flow.poll()[0]["call_id"] == "sub/before"
    sidecar.write_text(
        json.dumps(
            {
                "parent_call_id": "sub",
                "kind": "child_summary",
                "payload": {"synthesis": "сводка"},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    _sidecar(sidecar, [_nested_call("sub", "after", "exec_command", "{}")])
    reports = flow.poll()
    assert [item["call_id"] for item in reports] == ["sub/after"]


def test_title_path_is_relative_to_the_turn_cwd(tmp_path):
    """Пути в подписи — от корня проекта, как в ленте TUI (спека P3 §10)."""
    from gc_chat.responses.tool_flow import ToolFlow as Flow

    thread = tmp_path / "data_responses_t.jsonl"
    thread.write_text("", encoding="utf-8")
    flow = Flow(str(thread), root="/work/app")
    call = {
        "type": "function_call",
        "name": "fs_read_file",
        "call_id": "c1",
        "arguments": json.dumps({"path": "/work/app/src/a.py"}),
    }
    _write(thread, [call])
    sidecar = tmp_path / "data_subagents_responses_t.jsonl"
    with open(sidecar, "a", encoding="utf-8") as handle:
        record = {"parent_call_id": "c1", "kind": "call", "payload": call}
        handle.write(json.dumps(record) + "\n")
    titles = [report["title"] for report in flow.poll()]
    assert titles == ["fs_read_file(src/a.py)", "fs_read_file(src/a.py)"]
