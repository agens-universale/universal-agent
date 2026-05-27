import json
from types import SimpleNamespace

from states import SharedStateData
from tools.simple_tools.__init__ import get_simple_tools_data
from tools.simple_tools.working_memory import (
    DEFAULT_TOTAL_BUDGET_CHARS,
    REGISTER_CONSTRAINTS,
    REGISTER_GOAL,
    REGISTER_KNOWLEDGE,
    REGISTER_STATE,
    WorkingMemory,
)


def _make_state(working_memory=None):
    state = SimpleNamespace()
    state.shared = SharedStateData(
        working_memory=working_memory if working_memory is not None else {}
    )
    return state


def _call(state, **kwargs):
    tool = WorkingMemory(state=state)
    return json.loads(tool(**kwargs))


def test_list_returns_all_registers_with_sizes():
    res = _call(_make_state(), action="list")
    assert res["ok"] is True
    assert res["action"] == "list"
    assert set(res["registers"].keys()) == {
        REGISTER_GOAL,
        REGISTER_CONSTRAINTS,
        REGISTER_STATE,
        REGISTER_KNOWLEDGE,
    }
    assert res["total_chars"] == 0


def test_read_returns_empty_string_for_unset_register():
    res = _call(_make_state(), action="read", register=REGISTER_GOAL)
    assert res["ok"] is True
    assert res["content"] == ""


def test_append_and_read_register():
    state = _make_state()
    appended = _call(
        state,
        action="append",
        register=REGISTER_STATE,
        reason="фиксируем прогресс",
        content="- done: implement parser",
    )
    assert appended["ok"] is True
    assert "implement parser" in state.shared.working_memory[REGISTER_STATE]

    read_back = _call(state, action="read", register=REGISTER_STATE)
    assert read_back["ok"] is True
    assert "implement parser" in read_back["content"]


def test_rewrite_replaces_content():
    state = _make_state({REGISTER_KNOWLEDGE: "old"})
    res = _call(
        state,
        action="rewrite",
        register=REGISTER_KNOWLEDGE,
        reason="compact memory",
        content="new stable fact",
    )
    assert res["ok"] is True
    assert state.shared.working_memory[REGISTER_KNOWLEDGE] == "new stable fact"


def test_append_requires_reason_and_content():
    state = _make_state()
    no_reason = _call(
        state,
        action="append",
        register=REGISTER_STATE,
        reason=" ",
        content="delta",
    )
    assert no_reason["ok"] is False
    assert no_reason["error"]["type"] == "invalid_reason"

    no_content = _call(
        state,
        action="append",
        register=REGISTER_STATE,
        reason="update state",
        content=" ",
    )
    assert no_content["ok"] is False
    assert no_content["error"]["type"] == "empty_content"


def test_invalid_register_rejected():
    res = _call(
        _make_state(),
        action="read",
        register="journal",
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "invalid_register"


def test_register_limit_exceeded():
    ctx = SimpleNamespace(config_dict={"working_memory_params": {"max_chars_goal": 10}})
    tool = WorkingMemory(ctx=ctx, state=_make_state())
    res = json.loads(
        tool(
            action="rewrite",
            register=REGISTER_GOAL,
            reason="set goal",
            content="x" * 11,
        )
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "limit_exceeded"


def test_total_budget_limit_exceeded():
    state = _make_state({REGISTER_GOAL: "a" * 49})
    assert DEFAULT_TOTAL_BUDGET_CHARS == 20_000
    tool = WorkingMemory(
        ctx=SimpleNamespace(config_dict={"working_memory_params": {"total_budget_chars": 50}}),
        state=state,
    )
    res = json.loads(
        tool(
            action="append",
            register=REGISTER_GOAL,
            reason="new task",
            content="extra",
        )
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "total_budget_exceeded"


def test_no_state_error():
    res = json.loads(WorkingMemory(state=None)(action="list"))
    assert res["ok"] is False
    assert res["error"]["type"] == "no_state"


def test_registered_in_simple_tools_data():
    tools = get_simple_tools_data({})
    assert "working_memory" in tools
    assert tools["working_memory"]["tool_fabric"] is WorkingMemory
