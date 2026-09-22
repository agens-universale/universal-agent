"""trace_mapper: session_todo → kind=todo (чек-лист), output глушится."""
import json

from gc_tui.services.trace_mapper import map_message

ARGS = {"todos": [
    {"content": "Изучить код", "status": "completed"},
    {"content": "Написать тест", "status": "in_progress"},
    {"content": "Прогнать pytest", "status": "pending"},
]}


def _call(arguments) -> dict:
    # Реальная форма function_call из data_*.jsonl (chat_completions):
    # arguments — JSON-строка.
    return {
        "id": "m1", "type": "function_call", "name": "session_todo",
        "call_id": "call_1", "arguments": json.dumps(arguments, ensure_ascii=False),
    }


def test_call_maps_to_todo_kind_with_checklist():
    ev = map_message(_call(ARGS), no=1, ts="t")
    assert ev is not None
    assert ev.kind == "todo"
    assert ev.title == "session_todo"
    lines = ev.summary.splitlines()
    assert lines[0] == "чек-лист 1/3"
    assert lines[1] == "  ✓ Изучить код"
    assert lines[2] == "  → Написать тест"
    assert lines[3] == "  ○ Прогнать pytest"
    assert ev.tool_name == "session_todo"
    assert ev.call_id == "call_1"


def test_empty_list_maps_to_cleared():
    ev = map_message(_call({"todos": []}), no=1, ts="t")
    assert ev.kind == "todo"
    assert ev.summary == "чек-лист очищен"


def test_partially_broken_list_is_not_a_checklist():
    """Один кривой пункт → вызов не чек-лист, а обычная call-строка.

    Тул такой вызов отклоняет целиком и список не меняет; рисовать при этом
    «чек-лист 0/1» (или «очищен») значило бы врать о состоянии."""
    ev = map_message(
        _call({"todos": [{"content": "ok", "status": "pending"}, {"status": "bad"}]}),
        no=1, ts="t",
    )
    assert ev.kind == "call"


def test_output_suppressed_by_name():
    ev = map_message(
        {"id": "m2", "type": "function_call_output", "name": "session_todo",
         "call_id": "call_1", "output": "{}"},
        no=2, ts="t",
    )
    assert ev is None


def test_output_suppressed_by_tool_key_chat_completions():
    # chat_completions: у output нет name, но JSON содержит "tool".
    out = json.dumps({"tool": "session_todo", "ok": True, "count": 3})
    ev = map_message(
        {"id": "m3", "type": "function_call_output", "call_id": "call_1", "output": out},
        no=3, ts="t",
    )
    assert ev is None


def test_render_content_first_line_combines_title_and_summary():
    # Контракт main.py: content = f"{title}   {summary}".
    ev = map_message(_call(ARGS), no=1, ts="t")
    content = f"{ev.title}   {ev.summary}"
    assert content.splitlines()[0] == "session_todo   чек-лист 1/3"


def test_garbage_arguments_do_not_map_to_todo_kind():
    """Битые аргументы -> обычное call-событие (механика покажет статус),
    а не чек-лист с враньём «очищен»."""
    from gc_tui.services.trace_mapper import map_message
    ev = map_message(
        {"type": "function_call", "name": "session_todo", "call_id": "c1",
         "arguments": "оборванный json{{{"},
        no=1, ts="t",
    )
    assert ev is not None and ev.kind == "call"


def test_failed_output_is_not_suppressed():
    """ok:false-результат session_todo НЕ глушится: ошибка вызова видна."""
    import json as _json

    from gc_tui.services.trace_mapper import map_message
    out = _json.dumps({"tool": "session_todo", "ok": False,
                       "error": {"type": "invalid_todos", "message": "bad"},
                       "message": "Checklist NOT changed."})
    ev = map_message(
        {"type": "function_call_output", "call_id": "c1", "output": out},
        no=2, ts="t",
    )
    assert ev is not None


def test_ok_output_still_suppressed():
    import json as _json

    from gc_tui.services.trace_mapper import map_message
    out = _json.dumps({"tool": "session_todo", "ok": True, "count": 2,
                       "message": "Checklist replaced: 2 items."})
    ev = map_message(
        {"type": "function_call_output", "call_id": "c1", "output": out},
        no=2, ts="t",
    )
    assert ev is None
