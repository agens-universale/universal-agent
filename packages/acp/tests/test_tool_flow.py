"""Хвост jsonl → карточка тула, без повтора уже записанного."""

import json
import os

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
            "title": "exec_command(pytest -q)",
            "status": "in_progress",
            "name": "exec_command",
            "rawInput": {"command": "pytest -q"},
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
            "title": "exec_command(ls)",
            "status": "in_progress",
            "name": "exec_command",
            "rawInput": {"command": "ls"},
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
            "title": "subagent_explore(найти)",
            "status": "in_progress",
            "name": "subagent_explore",
            "rawInput": {"task": "найти"},
        },
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "sub:c1",
            "title": "fs_read_file(a.py)",
            "status": "in_progress",
            "name": "fs_read_file",
            "rawInput": {"path": "a.py"},
            "locations": [{"path": os.path.abspath("a.py")}],
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub:c1",
            "status": "completed",
            "content": [_preview('{"ok":true}')],
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub",
            "status": "completed",
            "content": [_preview('{"ok":true}')],
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
            "title": "exec_command(ls)",
            "status": "in_progress",
            "name": "exec_command",
            "rawInput": {"command": "ls"},
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub:c1",
            "status": "failed",
            "content": [_preview("boom")],
        },
    ]


def test_current_call_id_before_poll_matches_the_later_card(tmp_path):
    path = tmp_path / "data_acp_s.jsonl"
    flow = ToolFlow(str(path))
    path.write_text(
        _line(
            {
                "type": "function_call",
                "name": "fs_read_file",
                "call_id": "c1",
                "arguments": '{"path": "a.py"}',
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert flow.current_call_id() == "c1"
    assert flow.poll()[0]["toolCallId"] == "c1"


def test_current_call_id_is_the_first_unfinished_parent_call(tmp_path):
    path = tmp_path / "data_acp_s.jsonl"
    flow = ToolFlow(str(path))
    path.write_text(
        _line(
            {
                "type": "function_call",
                "name": "fs_read_file",
                "call_id": "first",
                "arguments": "{}",
            }
        )
        + "\n"
        + _line(
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "second",
                "arguments": "{}",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert flow.current_call_id() == "first"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            _line(
                {
                    "type": "function_call_output",
                    "call_id": "first",
                    "output": '{"ok": true}',
                }
            )
            + "\n"
        )
    assert flow.current_call_id() == "second"


def test_current_call_id_is_the_latest_open_nested_call(tmp_path):
    thread = tmp_path / "data_acp_s.jsonl"
    sidecar = tmp_path / "data_subagents_acp_s.jsonl"
    flow = ToolFlow(str(thread), sidecar_path=str(sidecar))
    thread.write_text(
        _line(
            {
                "type": "function_call",
                "name": "subagent_explore",
                "call_id": "sub",
                "arguments": "{}",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    sidecar.write_text(
        _sidecar(
            {
                "parent_call_id": "sub",
                "kind": "call",
                "payload": {
                    "type": "function_call",
                    "name": "fs_read_file",
                    "call_id": "c1",
                    "arguments": "{}",
                },
            }
        )
        + "\n"
        + _sidecar(
            {
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
                "parent_call_id": "sub",
                "kind": "call",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "c2",
                    "arguments": "{}",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert flow.current_call_id() == "sub:c2"


def test_current_call_id_ignores_the_previous_prompt(tmp_path):
    path = tmp_path / "data_acp_s.jsonl"
    path.write_text(
        _line(
            {
                "type": "function_call",
                "name": "fs_read_file",
                "call_id": "old",
                "arguments": "{}",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    flow = ToolFlow(str(path))
    assert flow.current_call_id() is None
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            _line(
                {
                    "type": "function_call",
                    "name": "fs_read_file",
                    "call_id": "new",
                    "arguments": "{}",
                }
            )
            + "\n"
        )
    assert flow.current_call_id() == "new"


def _grow_past(path, old_size: int) -> None:
    """Дописать журнал так, чтобы он стал длиннее среза до компакции."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            _line({"role": "assistant", "content": "y" * (old_size + 100)})
            + "\n"
        )
    assert path.stat().st_size > old_size


def test_current_call_id_keeps_poll_rewind_after_journal_grows(tmp_path):
    path = tmp_path / "data_acp_s.jsonl"
    path.write_text(
        _line({"role": "assistant", "content": "x" * 4000}) + "\n",
        encoding="utf-8",
    )
    flow = ToolFlow(str(path))
    old_size = path.stat().st_size
    path.write_text(
        _line(
            {
                "role": "user",
                "content": '<technical type="dialog_summary">сводка</technical>',
            }
        )
        + "\n"
        + _line(
            {
                "type": "function_call",
                "name": "fs_write_file",
                "call_id": "edit",
                "arguments": '{"path": "a.py"}',
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert flow.poll()[0]["toolCallId"] == "edit"
    _grow_past(path, old_size)
    assert flow.current_call_id() == "edit"


def test_current_call_id_remembers_its_own_rewind_after_journal_grows(
    tmp_path,
):
    path = tmp_path / "data_acp_s.jsonl"
    path.write_text(
        _line({"role": "assistant", "content": "x" * 4000}) + "\n",
        encoding="utf-8",
    )
    flow = ToolFlow(str(path))
    old_size = path.stat().st_size
    path.write_text(
        _line(
            {
                "type": "function_call",
                "name": "fs_write_file",
                "call_id": "edit",
                "arguments": '{"path": "a.py"}',
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert flow.current_call_id() == "edit"
    _grow_past(path, old_size)
    assert flow.current_call_id() == "edit"


def test_nested_call_id_keeps_rewind_after_sidecar_grows(tmp_path):
    thread = tmp_path / "data_acp_s.jsonl"
    sidecar = tmp_path / "data_subagents_acp_s.jsonl"
    thread.write_text("", encoding="utf-8")
    sidecar.write_text(
        _sidecar(
            {
                "parent_call_id": "sub",
                "kind": "call",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "old",
                    "arguments": "{}",
                },
            }
        )
        + "\n"
        + _line({"role": "assistant", "content": "x" * 4000})
        + "\n",
        encoding="utf-8",
    )
    flow = ToolFlow(str(thread), sidecar_path=str(sidecar))
    old_size = sidecar.stat().st_size
    sidecar.write_text(
        _sidecar(
            {
                "parent_call_id": "sub",
                "kind": "call",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "c2",
                    "arguments": "{}",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert flow.poll()[0]["toolCallId"] == "sub:c2"
    _grow_past(sidecar, old_size)
    assert flow.current_call_id() == "sub:c2"


def _preview(text):
    return {"type": "content", "content": {"type": "text", "text": text}}


def _call(name, call_id, arguments):
    return {
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": arguments,
    }


def test_call_card_is_the_signature_without_kind_or_content():
    seen: set[str] = set()
    report = report_from_record(
        _call(
            "fs_read_file",
            "c1",
            json.dumps({"path": "a.py", "offset": 10}),
        ),
        seen,
    )
    assert report == {
        "sessionUpdate": "tool_call",
        "toolCallId": "c1",
        "title": "fs_read_file(a.py)",
        "status": "in_progress",
        "name": "fs_read_file",
        "rawInput": {"path": "a.py", "offset": 10},
        "locations": [{"path": os.path.abspath("a.py"), "line": 10}],
    }
    assert "kind" not in report
    assert "content" not in report


def test_unparsed_arguments_omit_raw_input_and_the_card_stays():
    seen: set[str] = set()
    report = report_from_record(
        _call("exec_command", "c1", "not-json"),
        seen,
    )
    assert report["sessionUpdate"] == "tool_call"
    assert report["name"] == "exec_command"
    assert "rawInput" not in report
    assert "locations" not in report
    assert "content" not in report


def test_non_object_arguments_omit_raw_input():
    seen: set[str] = set()
    report = report_from_record(
        _call("exec_command", "c1", "[1, 2]"),
        seen,
    )
    assert "rawInput" not in report
    assert report["title"] == "exec_command"


def test_long_argument_strings_are_cut_and_the_call_is_left_intact():
    arguments = {
        "command": "x" * 6000,
        "n": 3,
        "ok": True,
        "gap": None,
        "nested": {"body": "y" * 6000},
        "items": ["z" * 6000, 1],
    }
    seen: set[str] = set()
    report = report_from_record(
        _call("exec_command", "c1", arguments),
        seen,
    )
    raw = report["rawInput"]
    assert raw["command"] == "x" * 5000
    assert raw["nested"]["body"] == "y" * 5000
    assert raw["items"] == ["z" * 5000, 1]
    assert raw["n"] == 3
    assert raw["ok"] is True
    assert raw["gap"] is None
    assert arguments["command"] == "x" * 6000
    assert arguments["nested"]["body"] == "y" * 6000
    assert "locations" not in report
    assert "content" not in report


def test_read_file_location_is_absolute_from_the_process_cwd():
    seen: set[str] = set()
    report = report_from_record(
        _call("fs_read_file", "c1", {"path": "dir/a.py", "offset": 1}),
        seen,
    )
    assert report["locations"] == [
        {"path": os.path.abspath("dir/a.py"), "line": 1}
    ]


def test_path_without_a_read_offset_has_no_line():
    seen: set[str] = set()
    report = report_from_record(
        _call("fs_write_file", "c1", {"path": "a.py", "offset": 4}),
        seen,
    )
    assert report["locations"] == [{"path": os.path.abspath("a.py")}]
    assert "line" not in report["locations"][0]


def test_missing_path_still_opens_a_card():
    seen: set[str] = set()
    report = report_from_record(
        _call("exec_command", "c1", {"command": "pytest -q"}),
        seen,
    )
    assert report["rawInput"] == {"command": "pytest -q"}
    assert "locations" not in report
    assert "content" not in report


def test_bool_offset_is_not_a_line():
    seen: set[str] = set()
    report = report_from_record(
        _call("fs_read_file", "c1", {"path": "a.py", "offset": True}),
        seen,
    )
    assert report["locations"] == [{"path": os.path.abspath("a.py")}]


def test_completion_sends_status_and_a_short_preview():
    seen: set[str] = set()
    report_from_record(
        _call("fs_read_file", "c1", {"path": "a.py", "offset": 2}),
        seen,
    )
    body = "a" * 6000
    update = report_from_record(
        {
            "type": "function_call_output",
            "call_id": "c1",
            "output": json.dumps(
                {"ok": False, "error": body, "start_line": 9}
            ),
        },
        seen,
    )
    assert update["status"] == "failed"
    assert update["toolCallId"] == "c1"
    text = update["content"][0]["content"]["text"]
    assert text.startswith('{"ok":false,"error":"')
    assert len(text) == 5000
    assert "start_line" not in text
    assert "rawOutput" not in update
    assert "locations" not in update
    assert "name" not in update
    assert "rawInput" not in update
    assert "kind" not in update


def test_empty_result_completes_without_content():
    seen: set[str] = set()
    report_from_record(_call("fs_read_file", "c1", {"path": "a.py"}), seen)
    update = report_from_record(
        {"type": "function_call_output", "call_id": "c1", "output": ""},
        seen,
    )
    assert update == {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "c1",
        "status": "completed",
    }


def test_stored_output_envelope_is_previewed_as_it_lies():
    envelope = {
        "type": "stored_tool_output",
        "output_ref": "ref-1",
        "content": ("q" * 8000) + "TAIL-MARKER",
    }
    seen: set[str] = set()
    report_from_record(_call("fs_read_file", "c1", {"path": "a.py"}), seen)
    update = report_from_record(
        {
            "type": "function_call_output",
            "call_id": "c1",
            "output": json.dumps(envelope),
        },
        seen,
    )
    text = update["content"][0]["content"]["text"]
    assert len(text) == 5000
    assert text.startswith('{"type":"stored_tool_output"')
    assert "TAIL-MARKER" not in text


def test_hidden_tools_do_not_open_or_close_a_card():
    seen: set[str] = set()
    for name in (
        "turn_finish",
        "turn_ask_human",
        "turn_intermediate_reply",
        "session_think",
        "turn_propose_plan",
    ):
        assert (
            report_from_record(_call(name, name, {"note": "x"}), seen) is None
        )
        assert (
            report_from_record(
                {
                    "type": "function_call_output",
                    "call_id": name,
                    "output": '{"ok": true}',
                },
                seen,
            )
            is None
        )
    assert seen == set()


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


def test_title_path_is_relative_to_the_session_cwd(tmp_path):
    """Пути в подписи — от корня проекта, как в ленте TUI (спека P3 §10)."""
    thread = tmp_path / "data_acp_s.jsonl"
    sidecar = tmp_path / "data_subagents_acp_s.jsonl"
    thread.write_text("", encoding="utf-8")
    flow = ToolFlow(str(thread), sidecar_path=str(sidecar), root="/work/app")
    call = {
        "type": "function_call",
        "name": "fs_read_file",
        "call_id": "c1",
        "arguments": '{"path": "/work/app/src/a.py"}',
    }
    thread.write_text(_line(call) + "\n", encoding="utf-8")
    nested = {**call, "call_id": "n1"}
    sidecar.write_text(
        json.dumps({"parent_call_id": "c1", "kind": "call", "payload": nested})
        + "\n",
        encoding="utf-8",
    )
    titles = [update["title"] for update in flow.poll()]
    assert titles == ["fs_read_file(src/a.py)", "fs_read_file(src/a.py)"]
