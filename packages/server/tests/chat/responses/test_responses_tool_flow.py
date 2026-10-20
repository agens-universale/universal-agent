"""Карточка тула из хвоста треда: без тел, без служебных тулов."""

import json

from gc_server.chat.tool_flow import ToolFlow
from gc_core.threads.facade import Threads
from gc_core.threads.models import ThreadKey
from gc_core.threads.session_port import SessionPort
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore

KEY = ThreadKey(system="responses", thread="t")


def _threads():
    return Threads(MemoryThreadStore())


async def _write(threads, payloads: list[dict]) -> None:
    """Сообщения хода — каждое своей записью, как шаги исполнителя."""
    for payload in payloads:
        await threads.append_messages(KEY, [payload])


async def _sidecar(threads, records: list[dict]) -> None:
    port = SessionPort(threads.store)
    for record in records:
        await port.aappend_subagent_trace(
            KEY.system,
            KEY.thread,
            parent_call_id=record["parent_call_id"],
            child_run_id=record.get("child_run_id", "child"),
            kind=record["kind"],
            payload=record["payload"],
        )


async def test_card_has_title_and_no_bodies():
    threads = _threads()
    flow = await ToolFlow.start(threads, KEY)
    await _write(
        threads,
        [
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "c1",
                "arguments": json.dumps({"path": "src/app.py"}),
            }
        ],
    )
    opened = await flow.poll()
    assert len(opened) == 1
    card = opened[0]
    assert card["op"] == "open"
    assert card["name"] == "read_file"
    assert card["title"].startswith("read_file(")
    assert "path" in card["title"]
    assert "arguments" not in card
    assert "output" not in card
    await _write(
        threads,
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
    closed = await flow.poll()
    assert closed == [{"op": "close", "call_id": "c1", "status": "failed"}]
    assert "сырое тело" not in json.dumps(closed)


async def test_success_output_completes_and_hides_body():
    threads = _threads()
    flow = await ToolFlow.start(threads, KEY)
    await _write(
        threads,
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
    reports = await flow.poll()
    assert reports[0]["title"] == "list_dir"
    assert reports[1]["status"] == "completed"
    assert "a.py" not in json.dumps(reports)


async def test_hidden_tools_and_duplicate_call_id():
    threads = _threads()
    flow = await ToolFlow.start(threads, KEY)
    await _write(
        threads,
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
    reports = await flow.poll()
    assert [item["call_id"] for item in reports] == ["same"]
    blob = json.dumps(reports)
    assert "turn_finish" not in blob
    assert "turn_ask_human" not in blob
    assert "turn_intermediate_reply" not in blob
    assert "session_think" not in blob


async def test_offset_skips_lines_from_before_this_turn():
    threads = _threads()
    await _write(threads, [_call("read_file", "old")])
    flow = await ToolFlow.start(threads, KEY)
    await _write(threads, [_call("read_file", "new")])
    reports = await flow.poll()
    assert [item["call_id"] for item in reports] == ["new"]


async def test_poll_after_compaction_reads_the_new_history():
    threads = _threads()
    await _write(
        threads,
        [
            _call("read_file", "before"),
            {"role": "assistant", "content": "длинный хвост " * 40},
        ],
    )
    flow = await ToolFlow.start(threads, KEY)
    await _write(
        threads, [_call("read_file", "mid", json.dumps({"path": "b.py"}))]
    )
    assert (await flow.poll())[0]["call_id"] == "mid"

    summary = {
        "role": "user",
        "content": '<technical type="dialog_summary">сводка</technical>',
        "id": "summary",
    }
    await SessionPort(threads.store).aarchive_and_replace(
        KEY.system, KEY.thread, [summary]
    )
    await _write(
        threads,
        [_call("exec_command", "after", json.dumps({"command": "ls"}))],
    )
    reports = await flow.poll()
    assert [item["call_id"] for item in reports] == ["after"]
    assert reports[0]["op"] == "open"
    assert reports[0]["title"] == "exec_command(command='ls')"
    assert "сводка" not in json.dumps(reports)


def _call(name: str, call_id: str, arguments: str = "{}") -> dict:
    return {
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": arguments,
    }


def _nested_call(parent: str, call_id: str, name: str, arguments: str) -> dict:
    return {
        "parent_call_id": parent,
        "child_run_id": "child",
        "kind": "call",
        "payload": _call(name, call_id, arguments),
    }


async def test_sidecar_tool_is_a_card_under_the_parent():
    threads = _threads()
    flow = await ToolFlow.start(threads, KEY)
    await _sidecar(
        threads,
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
    reports = await flow.poll()
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


async def test_same_child_call_id_under_two_parents_are_two_cards():
    threads = _threads()
    flow = await ToolFlow.start(threads, KEY)
    await _sidecar(
        threads,
        [
            _nested_call("left", "c1", "read_file", "{}"),
            _nested_call("right", "c1", "exec_command", "{}"),
        ],
    )
    reports = await flow.poll()
    assert [item["call_id"] for item in reports] == ["left/c1", "right/c1"]
    assert [item["parent_call_id"] for item in reports] == ["left", "right"]


async def test_sidecar_error_closes_the_card_as_failed():
    threads = _threads()
    flow = await ToolFlow.start(threads, KEY)
    await _sidecar(threads, [_nested_call("sub", "c1", "exec_command", "{}")])
    assert (await flow.poll())[0]["op"] == "open"
    await _sidecar(
        threads,
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
    assert await flow.poll() == [
        {"op": "close", "call_id": "sub/c1", "status": "failed"}
    ]


async def test_recreated_thread_still_reports_the_new_call():
    """Трасса начинается заново только с треда, созданного заново."""
    threads = _threads()
    flow = await ToolFlow.start(threads, KEY)
    await _sidecar(threads, [_nested_call("sub", "before", "read_file", "{}")])
    assert (await flow.poll())[0]["call_id"] == "sub/before"
    await threads.delete(KEY)
    await _sidecar(
        threads, [_nested_call("sub", "after", "exec_command", "{}")]
    )
    reports = await flow.poll()
    assert [item["call_id"] for item in reports] == ["sub/after"]
