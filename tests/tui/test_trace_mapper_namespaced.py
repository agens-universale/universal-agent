"""trace_mapper на НОВЫХ именах тулов (master namespaces) — рендер не ломается."""
import json
from gc_tui.services.trace_mapper import map_message


def _fc(name, args, call_id="c1"):
    return {"type": "function_call", "name": name, "call_id": call_id,
            "arguments": json.dumps(args)}


def test_turn_ask_human_detected_as_ask_human():
    ev = map_message(_fc("turn_ask_human", {"question": "Q?"}), no=1, ts="t")
    assert ev.kind == "ask_human"
    assert ev.tool_name == "ask_human"


def test_turn_finish_detected_as_submit_solution():
    ev = map_message(_fc("turn_finish", {"task_report": "done"}), no=1, ts="t")
    assert ev.kind == "agent"
    assert ev.tool_name == "submit_solution"


def test_turn_intermediate_reply_detected():
    ev = map_message(_fc("turn_intermediate_reply", {"answer": "hi"}), no=1, ts="t")
    assert ev.tool_name == "intermediate_reply"


def test_fs_read_file_arg_summary_uses_path():
    ev = map_message(_fc("fs_read_file", {"path": "src/x.py"}), no=1, ts="t")
    assert "src/x.py" in ev.summary


def test_ask_human_output_with_new_tool_field():
    out = {"type": "function_call_output", "call_id": "c1",
           "output": json.dumps({"tool": "turn_ask_human", "ok": True, "response": "ans"})}
    ev = map_message(out, no=2, ts="t")
    assert ev.tool_name == "ask_human"


def test_session_compact_call_renders_generic():
    # новый тул master: trace_mapper его имя не сравнивает → generic-рендер, без ремапа
    ev = map_message(_fc("session_compact", {"reason": "phase done"}), no=1, ts="t")
    assert ev is not None
    assert ev.kind == "call"
    assert "session_compact" in (ev.summary or "")


def test_session_compact_output_not_treated_as_final_answer():
    # в отличие от submit_solution/ask_human, вывод session_compact НЕ мьютится и
    # НЕ принимается за финальный ответ
    out = {"type": "function_call_output", "call_id": "c1",
           "output": json.dumps({"tool": "session_compact", "ok": True, "compacted": True})}
    ev = map_message(out, no=2, ts="t")
    assert ev is not None
    assert ev.kind != "agent"
