"""Карточка из jsonl: без тел, ``turn_finish`` наружу не выходит."""

import json

from gc_chat.completions.content import ContentMap
from gc_chat.completions.tool_flow import ToolFlow


def _write(path, payloads: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        for payload in payloads:
            record = {"type": "message", "payload": payload}
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def test_card_line_omits_arguments_and_hides_turn_finish(tmp_path):
    path = tmp_path / "thread.jsonl"
    flow = ToolFlow(str(path))
    _write(
        path,
        [
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
        ],
    )
    reports = flow.poll()
    assert len(reports) == 1
    mapper = ContentMap()
    for report in reports:
        mapper.apply_tool(report)
    mapper.finish("готово")
    assert "turn_finish" not in mapper.content
    assert "сырой json" not in mapper.content
    assert "read_file(path='src/app.py') — in_progress" in mapper.content
