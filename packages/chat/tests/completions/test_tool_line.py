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
    assert "read_file(src/app.py) — in_progress" in mapper.content


def test_title_path_is_relative_to_the_turn_cwd(tmp_path):
    """Пути в подписи — от корня проекта, как в ленте TUI (спека P3 §10)."""
    from gc_chat.completions.tool_flow import ToolFlow as Flow

    thread = tmp_path / "data_completions_t.jsonl"
    thread.write_text("", encoding="utf-8")
    flow = Flow(str(thread), root="/work/app")
    call = {
        "type": "function_call",
        "name": "fs_read_file",
        "call_id": "c1",
        "arguments": json.dumps({"path": "/work/app/src/a.py"}),
    }
    _write(thread, [call])
    sidecar = tmp_path / "data_subagents_completions_t.jsonl"
    with open(sidecar, "a", encoding="utf-8") as handle:
        record = {"parent_call_id": "c1", "kind": "call", "payload": call}
        handle.write(json.dumps(record) + "\n")
    titles = [report["title"] for report in flow.poll()]
    assert titles == ["fs_read_file(src/a.py)", "fs_read_file(src/a.py)"]
