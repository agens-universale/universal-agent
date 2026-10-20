"""Хвост треда → карточка тула, без повтора уже записанного."""

import json
import os

from gc_core.threads.facade import Threads
from gc_core.threads.models import ThreadKey
from gc_core.threads.session_port import SessionPort
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore

from gc_acp.tool_flow import ToolFlow, report_from_record

KEY = ThreadKey(system="acp", thread="s")


def _threads():
    return Threads(MemoryThreadStore())


def _call(name, call_id, arguments):
    return {
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": json.dumps(arguments),
    }


def _output(call_id, output):
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }


async def _write(threads, *messages):
    """Сообщения треда — каждое своей записью, как шаги хода."""
    for message in messages:
        await threads.append_messages(KEY, [message])


async def _subagent(threads, kind, payload):
    await SessionPort(threads.store).aappend_subagent_trace(
        KEY.system,
        KEY.thread,
        parent_call_id="sub",
        child_run_id="child",
        kind=kind,
        payload=payload,
    )


async def test_poll_skips_entries_already_written_and_hides_bookkeeping():
    threads = _threads()
    await _write(threads, _call("fs_read_file", "old", {"path": "old.py"}))
    flow = await ToolFlow.start(threads, KEY)
    await _write(
        threads,
        _call("exec_command", "c1", {"command": "pytest -q"}),
        _call("turn_ask_human", "ask", {}),
    )
    assert await flow.poll() == [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "c1",
            "title": "exec_command(command='pytest -q')",
            "status": "in_progress",
            "name": "exec_command",
            "rawInput": {"command": "pytest -q"},
        }
    ]
    assert await flow.poll() == []


async def test_poll_after_compaction_reads_the_new_history():
    threads = _threads()
    await _write(
        threads,
        _call("fs_read_file", "before", {"path": "a.py"}),
        {"role": "assistant", "content": "длинный хвост " * 40},
    )
    flow = await ToolFlow.start(threads, KEY)
    await _write(threads, _call("fs_read_file", "mid", {"path": "b.py"}))
    assert (await flow.poll())[0]["toolCallId"] == "mid"

    summary = {
        "role": "user",
        "content": '<technical type="dialog_summary">сводка</technical>',
        "id": "summary",
    }
    await SessionPort(threads.store).aarchive_and_replace(
        KEY.system, KEY.thread, [summary]
    )
    await _write(threads, _call("exec_command", "after", {"command": "ls"}))
    assert await flow.poll() == [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "after",
            "title": "exec_command(command='ls')",
            "status": "in_progress",
            "name": "exec_command",
            "rawInput": {"command": "ls"},
        }
    ]


async def test_poll_places_subagent_tools_between_call_and_result():
    threads = _threads()
    flow = await ToolFlow.start(threads, KEY)
    await _write(threads, _call("subagent_explore", "sub", {"task": "найти"}))
    await _subagent(
        threads, "call", _call("fs_read_file", "c1", {"path": "a.py"})
    )
    await _subagent(threads, "result", _output("c1", '{"ok": true}'))
    await _subagent(threads, "call", _call("turn_finish", "fin", {}))
    await _write(threads, _output("sub", '{"ok": true}'))
    assert await flow.poll() == [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "sub",
            "title": "subagent_explore(task='найти')",
            "status": "in_progress",
            "name": "subagent_explore",
            "rawInput": {"task": "найти"},
        },
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "sub:c1",
            "title": "fs_read_file(path='a.py')",
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
    assert await flow.poll() == []


async def test_poll_reads_thread_created_after_start():
    threads = _threads()
    flow = await ToolFlow.start(threads, KEY)
    assert await flow.poll() == []
    await _subagent(
        threads, "call", _call("exec_command", "c1", {"command": "ls"})
    )
    await _subagent(threads, "error", _output("c1", "boom"))
    assert await flow.poll() == [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "sub:c1",
            "title": "exec_command(command='ls')",
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


async def test_current_call_id_before_poll_matches_the_later_card():
    threads = _threads()
    flow = await ToolFlow.start(threads, KEY)
    await _write(threads, _call("fs_read_file", "c1", {"path": "a.py"}))
    assert await flow.current_call_id() == "c1"
    assert (await flow.poll())[0]["toolCallId"] == "c1"


async def test_current_call_id_is_the_first_unfinished_parent_call():
    threads = _threads()
    flow = await ToolFlow.start(threads, KEY)
    await _write(
        threads,
        _call("fs_read_file", "first", {}),
        _call("exec_command", "second", {}),
    )
    assert await flow.current_call_id() == "first"
    await _write(threads, _output("first", '{"ok": true}'))
    assert await flow.current_call_id() == "second"


async def test_current_call_id_is_the_latest_open_nested_call():
    threads = _threads()
    flow = await ToolFlow.start(threads, KEY)
    await _write(threads, _call("subagent_explore", "sub", {}))
    await _subagent(threads, "call", _call("fs_read_file", "c1", {}))
    await _subagent(threads, "result", _output("c1", '{"ok": true}'))
    await _subagent(threads, "call", _call("exec_command", "c2", {}))
    assert await flow.current_call_id() == "sub:c2"


async def test_current_call_id_ignores_the_previous_prompt():
    threads = _threads()
    await _write(threads, _call("fs_read_file", "old", {}))
    flow = await ToolFlow.start(threads, KEY)
    assert await flow.current_call_id() is None
    await _write(threads, _call("fs_read_file", "new", {}))
    assert await flow.current_call_id() == "new"


async def _compact(threads):
    """Сжатие истории посреди хода: история — одна сводка, версия растёт."""
    summary = {
        "role": "user",
        "content": '<technical type="dialog_summary">сводка</technical>',
        "id": "summary",
    }
    await SessionPort(threads.store).aarchive_and_replace(
        KEY.system, KEY.thread, [summary]
    )


async def _grow(threads):
    """Дописать историю после сжатия: длиннее, чем она была до него."""
    await _write(threads, {"role": "assistant", "content": "y" * 5000})


async def test_current_call_id_keeps_poll_rewind_after_journal_grows():
    threads = _threads()
    await _write(threads, {"role": "assistant", "content": "x" * 4000})
    flow = await ToolFlow.start(threads, KEY)
    await _compact(threads)
    await _write(threads, _call("fs_write_file", "edit", {"path": "a.py"}))
    assert (await flow.poll())[0]["toolCallId"] == "edit"
    await _grow(threads)
    assert await flow.current_call_id() == "edit"


async def test_current_call_id_remembers_its_own_rewind_after_journal_grows():
    threads = _threads()
    await _write(threads, {"role": "assistant", "content": "x" * 4000})
    flow = await ToolFlow.start(threads, KEY)
    await _compact(threads)
    await _write(threads, _call("fs_write_file", "edit", {"path": "a.py"}))
    assert await flow.current_call_id() == "edit"
    await _grow(threads)
    assert await flow.current_call_id() == "edit"


async def test_nested_call_id_survives_compaction_of_the_history():
    """Сжатие переписывает историю, а не поток субагента: вызов прошлого
    prompt там не всплывает, текущий не теряется."""
    threads = _threads()
    await _subagent(threads, "call", _call("exec_command", "old", {}))
    flow = await ToolFlow.start(threads, KEY)
    await _compact(threads)
    await _subagent(threads, "call", _call("exec_command", "c2", {}))
    assert (await flow.poll())[0]["toolCallId"] == "sub:c2"
    await _grow(threads)
    assert await flow.current_call_id() == "sub:c2"


def _preview(text):
    return {"type": "content", "content": {"type": "text", "text": text}}


def _raw_call(name, call_id, arguments):
    return {
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": arguments,
    }


def test_call_card_is_the_signature_without_kind_or_content():
    seen: set[str] = set()
    report = report_from_record(
        _raw_call(
            "fs_read_file",
            "c1",
            json.dumps({"path": "a.py", "offset": 10}),
        ),
        seen,
    )
    assert report == {
        "sessionUpdate": "tool_call",
        "toolCallId": "c1",
        "title": "fs_read_file(path='a.py', offset=10)",
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
        _raw_call("exec_command", "c1", "not-json"),
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
        _raw_call("exec_command", "c1", "[1, 2]"),
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
        _raw_call("exec_command", "c1", arguments),
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
        _raw_call("fs_read_file", "c1", {"path": "dir/a.py", "offset": 1}),
        seen,
    )
    assert report["locations"] == [
        {"path": os.path.abspath("dir/a.py"), "line": 1}
    ]


def test_path_without_a_read_offset_has_no_line():
    seen: set[str] = set()
    report = report_from_record(
        _raw_call("fs_write_file", "c1", {"path": "a.py", "offset": 4}),
        seen,
    )
    assert report["locations"] == [{"path": os.path.abspath("a.py")}]
    assert "line" not in report["locations"][0]


def test_missing_path_still_opens_a_card():
    seen: set[str] = set()
    report = report_from_record(
        _raw_call("exec_command", "c1", {"command": "pytest -q"}),
        seen,
    )
    assert report["rawInput"] == {"command": "pytest -q"}
    assert "locations" not in report
    assert "content" not in report


def test_bool_offset_is_not_a_line():
    seen: set[str] = set()
    report = report_from_record(
        _raw_call("fs_read_file", "c1", {"path": "a.py", "offset": True}),
        seen,
    )
    assert report["locations"] == [{"path": os.path.abspath("a.py")}]


def test_completion_sends_status_and_a_short_preview():
    seen: set[str] = set()
    report_from_record(
        _raw_call("fs_read_file", "c1", {"path": "a.py", "offset": 2}),
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
    report_from_record(_raw_call("fs_read_file", "c1", {"path": "a.py"}), seen)
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
    report_from_record(_raw_call("fs_read_file", "c1", {"path": "a.py"}), seen)
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
            report_from_record(_raw_call(name, name, {"note": "x"}), seen)
            is None
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
