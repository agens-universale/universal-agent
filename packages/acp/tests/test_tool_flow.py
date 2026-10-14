"""Хвост треда → карточка тула, без повтора уже записанного."""

import json

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
