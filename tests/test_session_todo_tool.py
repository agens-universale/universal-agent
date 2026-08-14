import json

from states import SharedStateData, State
from tools.registry import get_builtin_tools_data
from tools.session.todo import SessionTodo


def _state() -> State:
    return State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=100,
        system_prompt_id="v1",
        use_server_subagents=False,
        shared=SharedStateData(),
    )


ITEMS = [
    {"content": "Изучить код", "status": "completed"},
    {"content": "Написать тест", "status": "in_progress"},
    {"content": "Прогнать pytest", "status": "pending"},
]


def test_write_replaces_list_in_shared_state():
    state = _state()
    tool = SessionTodo(state=state)
    out = json.loads(tool(todos=list(ITEMS)))
    assert out["ok"] is True
    assert state.shared.session_todo == ITEMS
    # replace-семантика: второй вызов замещает целиком
    out2 = json.loads(tool(todos=[{"content": "Новый план", "status": "pending"}]))
    assert out2["ok"] is True
    assert state.shared.session_todo == [{"content": "Новый план", "status": "pending"}]


def test_response_contains_counts():
    state = _state()
    out = json.loads(SessionTodo(state=state)(todos=list(ITEMS)))
    assert out["count"] == 3
    assert out["completed"] == 1
    assert out["in_progress"] == 1
    assert out["pending"] == 1


def test_empty_list_clears():
    state = _state()
    tool = SessionTodo(state=state)
    tool(todos=list(ITEMS))
    out = json.loads(tool(todos=[]))
    assert out["ok"] is True
    assert out["count"] == 0
    assert state.shared.session_todo == []


def test_invalid_status_is_error_and_state_untouched():
    state = _state()
    tool = SessionTodo(state=state)
    tool(todos=list(ITEMS))
    out = json.loads(tool(todos=[{"content": "x", "status": "done"}]))
    assert out["ok"] is False
    assert out["error"]["type"] == "invalid_item"
    assert state.shared.session_todo == ITEMS  # прежний список не тронут


def test_empty_content_is_error():
    state = _state()
    out = json.loads(SessionTodo(state=state)(todos=[{"content": "  ", "status": "pending"}]))
    assert out["ok"] is False
    assert out["error"]["type"] == "invalid_item"


def test_todos_not_list_is_error():
    state = _state()
    out = json.loads(SessionTodo(state=state)(todos="1. do x"))
    assert out["ok"] is False
    assert out["error"]["type"] == "invalid_todos"


def test_no_state_is_error():
    out = json.loads(SessionTodo()(todos=[]))
    assert out["ok"] is False
    assert out["error"]["type"] == "no_state"


def test_registered_in_both_backend_modes():
    for mode in ("responses", "chat_completions"):
        tools = get_builtin_tools_data({"backend_api_mode": mode})
        assert "session_todo" in tools, mode


def test_respects_stop_tools():
    tools = get_builtin_tools_data({"stop_tools": ["session_todo"]})
    assert "session_todo" not in tools
