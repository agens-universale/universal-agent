from copy import deepcopy
from types import SimpleNamespace

import pytest
from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.state import SharedStateData
from gc_core.security.permission_store import PermissionStore
from gc_core.tools.subagents.runner_transfer import (
    apply_subagent_transfer,
    capture_subagent_transfer,
)
from gc_core.usage.account import UsageAccount


def fixture():
    state = SimpleNamespace(
        shared=SharedStateData(working_memory={"base": "one"}),
        usage=UsageAccount(),
        tool_calls=2,
    )
    ctx = SimpleNamespace(
        permission_store=PermissionStore(state.shared.confirm_rules)
    )
    return state, ctx


def test_child_transfer_keeps_parent_edits_and_permission_references():
    state, ctx = fixture()
    baseline = capture_subagent_transfer(state, ctx)
    child, childctx = fixture()
    child.shared.working_memory["child"] = "result"
    childctx.permission_store.record("read:file", True, session=True)
    childctx.permission_store.record("read:once", True, session=False)
    child.usage.add("subagent", 3, 2)
    child.tool_calls += 4
    result = capture_subagent_transfer(child, childctx)
    state.shared.working_memory["parent"] = "new input"
    state.usage.add("main", 1, 1)
    state.tool_calls += 1
    rules = state.shared.confirm_rules
    apply_subagent_transfer(state, ctx, baseline, result)
    assert state.shared.working_memory == {
        "base": "one",
        "child": "result",
        "parent": "new input",
    }
    assert state.usage.totals()["total"] == 7
    assert state.tool_calls == 7
    assert state.shared.confirm_rules is rules
    assert ctx.permission_store.decision_for("read:file") is True
    assert ctx.permission_store.decision_for("read:once") is True
    # A final checkpoint with the same cumulative values adds no usage/calls.
    apply_subagent_transfer(state, ctx, result, result)
    assert state.usage.totals()["total"] == 7
    assert state.tool_calls == 7


def test_conflicting_child_delta_is_atomic():
    state, ctx = fixture()
    baseline = capture_subagent_transfer(state, ctx)
    child, childctx = fixture()
    child.shared.working_memory["base"] = "child"
    result = capture_subagent_transfer(child, childctx)
    state.shared.working_memory["base"] = "parent"
    before = deepcopy(state.__dict__)
    with pytest.raises(
        ExecutionFailure, match="Concurrent subagent state change"
    ):
        apply_subagent_transfer(state, ctx, baseline, result)
    assert state.shared == before["shared"]
    assert state.tool_calls == before["tool_calls"]


def test_fetch_cache_order_survives_transfer():
    state, ctx = fixture()
    state.shared.fetch_url_cache = {"one": 1, "two": 2}
    baseline = capture_subagent_transfer(state, ctx)
    child, childctx = fixture()
    child.shared.fetch_url_cache = {"two": 2, "one": 1}
    apply_subagent_transfer(
        state, ctx, baseline, capture_subagent_transfer(child, childctx)
    )
    assert list(state.shared.fetch_url_cache) == ["two", "one"]


@pytest.mark.parametrize("skip", [False, True])
def test_real_parent_preparation_flag_roundtrips_for_child(skip):
    from gc_core.state_preparation import PreparedState
    from gc_core.states import State

    state = State.from_prepared(
        PreparedState("parent", {}, ""),
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=5,
        system_prompt_id="test",
        use_server_subagents=False,
    )
    state.skip_initial_user_ask_step_when_history = skip
    restored = State.from_execution_snapshot(state.to_execution_snapshot())
    assert restored.skip_initial_user_ask_step_when_history is skip
