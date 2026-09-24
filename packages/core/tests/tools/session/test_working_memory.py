import json
from types import SimpleNamespace

from gc_core.states import SharedStateData
from gc_core.tools.registry import get_builtin_tools_data
from gc_core.tools.session.working_memory import (
    DEFAULT_TOTAL_BUDGET_CHARS,
    REGISTER_CONSTRAINTS,
    REGISTER_GOAL,
    REGISTER_KNOWLEDGE,
    REGISTER_STATE,
    WorkingMemory,
)
from gc_testkit.helpers.tool_ctx import CallableTool


def _make_state(working_memory=None):
    state = SimpleNamespace()
    state.shared = SharedStateData(
        working_memory=working_memory if working_memory is not None else {}
    )
    return state


def _call(state, **kwargs):
    tool = CallableTool(WorkingMemory(state=state))
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


def test_write_replaces_content():
    state = _make_state({REGISTER_KNOWLEDGE: "old"})
    res = _call(
        state,
        action="write",
        register=REGISTER_KNOWLEDGE,
        reason="compact memory",
        content="new stable fact",
    )
    assert res["ok"] is True
    assert state.shared.working_memory[REGISTER_KNOWLEDGE] == "new stable fact"


def test_append_reason_optional_content_required():
    state = _make_state()
    no_reason = _call(
        state,
        action="append",
        register=REGISTER_STATE,
        reason=" ",
        content="- done",
    )
    assert no_reason["ok"] is True
    assert "- done" in state.shared.working_memory[REGISTER_STATE]

    no_content = _call(
        state,
        action="append",
        register=REGISTER_STATE,
        reason="update state",
        content=" ",
    )
    assert no_content["ok"] is False
    assert no_content["error"]["type"] == "empty_content"


def test_append_without_reason_kwarg_ok():
    state = _make_state()
    res = _call(
        state,
        action="append",
        register=REGISTER_STATE,
        content="- done: implement parser",
    )
    assert res["ok"] is True
    assert "implement parser" in state.shared.working_memory[REGISTER_STATE]


def test_write_without_reason_ok():
    state = _make_state({REGISTER_KNOWLEDGE: "old"})
    res = _call(
        state,
        action="write",
        register=REGISTER_KNOWLEDGE,
        content="new stable fact",
    )
    assert res["ok"] is True
    assert state.shared.working_memory[REGISTER_KNOWLEDGE] == "new stable fact"


def test_prompt_description_reason_optional():
    prompt = WorkingMemory.prompt_description().lower()
    assert "reason" in prompt
    assert "optional" in prompt
    assert "content" in prompt


def test_schema_requires_action_and_register():
    required = WorkingMemory.tool_description()["parameters"]["required"]
    assert required == ["action", "register"]
    prompt = WorkingMemory.prompt_description()
    assert '"required":["action","register"]' in prompt.replace(" ", "")


def test_invalid_register_rejected():
    res = _call(
        _make_state(),
        action="read",
        register="journal",
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "invalid_register"


def test_write_without_register_missing_register():
    res = _call(
        _make_state(),
        action="write",
        content="should not land without register",
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "missing_register"
    assert "register is required" in res["error"]["message"]


def test_empty_register_missing_register():
    res = _call(
        _make_state(),
        action="append",
        register="  ",
        content="- delta",
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "missing_register"


def test_register_limit_exceeded():
    ctx = SimpleNamespace(
        config_dict={"working_memory_params": {"max_chars_goal": 10}}
    )
    tool = CallableTool(WorkingMemory(ctx=ctx, state=_make_state()))
    res = json.loads(
        tool(
            action="write",
            register=REGISTER_GOAL,
            reason="set goal",
            content="x" * 11,
        )
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "register_limit_exceeded"


def test_total_budget_limit_exceeded():
    state = _make_state({REGISTER_GOAL: "a" * 49})
    assert DEFAULT_TOTAL_BUDGET_CHARS == 20_000
    tool = CallableTool(
        WorkingMemory(
            ctx=SimpleNamespace(
                config_dict={
                    "working_memory_params": {"total_budget_chars": 50}
                }
            ),
            state=state,
        )
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
    res = json.loads(CallableTool(WorkingMemory(state=None))(action="list"))
    assert res["ok"] is False
    assert res["error"]["type"] == "no_state"


def test_registered_in_builtin_tools_data():
    tools = get_builtin_tools_data({})
    assert "session_memory" in tools
    assert tools["session_memory"]["tool_fabric"] is WorkingMemory
