"""Карточка из хвоста треда: без тел, ``turn_finish`` наружу не выходит."""

import json

from gc_server.chat.completions.content import ContentMap
from gc_server.chat.tool_flow import ToolFlow
from gc_core.threads.facade import Threads
from gc_core.threads.models import ThreadKey
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore

KEY = ThreadKey(system="completions", thread="t")


async def test_card_line_omits_arguments_and_hides_turn_finish():
    threads = Threads(MemoryThreadStore())
    flow = await ToolFlow.start(threads, KEY)
    for payload in (
        {
            "type": "function_call",
            "name": "turn_finish",
            "call_id": "fin",
            "arguments": json.dumps({"task_report": "сырой json"}),
        },
        {
            "type": "function_call",
            "name": "read_file",
            "call_id": "c1",
            "arguments": json.dumps({"path": "src/app.py"}),
        },
    ):
        await threads.append_messages(KEY, [payload])
    reports = await flow.poll()
    assert len(reports) == 1
    mapper = ContentMap()
    for report in reports:
        mapper.apply_tool(report)
    mapper.finish("готово")
    assert "turn_finish" not in mapper.content
    assert "сырой json" not in mapper.content
    assert "read_file(path='src/app.py') — in_progress" in mapper.content


async def test_card_hides_harness_key_and_keeps_failure():
    threads = Threads(MemoryThreadStore())
    flow = await ToolFlow.start(threads, KEY)
    hint = "Loop detector: same arguments again"
    await threads.append_messages(
        KEY,
        [
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "c1",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": json.dumps(
                    {
                        "ok": False,
                        "message": "denied",
                        "technical_system_messages": [hint],
                        "technical_system_messages_1": ["mode"],
                    }
                ),
            },
        ],
    )
    mapper = ContentMap()
    for report in await flow.poll():
        mapper.apply_tool(report)
    assert "read_file — failed" in mapper.content
    assert hint not in mapper.content
    assert "technical_system_messages" not in mapper.content
