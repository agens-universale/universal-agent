from copy import deepcopy
from types import SimpleNamespace

import pytest
from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.state import SharedStateData
from gc_core.model_router import CircuitBreaker
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


def test_parallel_children_judge_denials_add_up_instead_of_conflicting():
    """Два параллельных субагента в auto копят отказы судьи в общем счёте
    сессии одновременно. Счётчики складываются, как счёт вызовов, а не
    роняют прогон родителя конфликтом."""
    state, ctx = fixture()
    baseline = capture_subagent_transfer(state, ctx)
    # первый ребёнок уже вернул два отказа
    state.shared.judge_denials_total = 2
    state.shared.judge_denials_in_a_row = 2
    child, childctx = fixture()
    child.shared.judge_denials_total = 1
    child.shared.judge_denials_in_a_row = 1
    child.shared.judge_paused = True
    apply_subagent_transfer(
        state, ctx, baseline, capture_subagent_transfer(child, childctx)
    )
    assert state.shared.judge_denials_total == 3
    assert state.shared.judge_denials_in_a_row == 3
    assert state.shared.judge_paused is True


def test_equal_parallel_denials_are_not_lost():
    """Два ребёнка по одному отказу: 0→1 и 0→1 — это 2, а не 1."""
    state, ctx = fixture()
    baseline = capture_subagent_transfer(state, ctx)
    state.shared.judge_denials_total = 1  # первый ребёнок уже вернул свой
    child, childctx = fixture()
    child.shared.judge_denials_total = 1
    apply_subagent_transfer(
        state, ctx, baseline, capture_subagent_transfer(child, childctx)
    )
    assert state.shared.judge_denials_total == 2


def test_a_child_reset_wins_over_sibling_increments():
    """Сброс серии у ребёнка (судья разрешил) не вычитается из чужих отказов:
    счёт берётся у ребёнка — серия прервана."""
    state, ctx = fixture()
    state.shared.judge_denials_in_a_row = 2
    baseline = capture_subagent_transfer(state, ctx)
    state.shared.judge_denials_in_a_row = 3  # сосед отказал ещё раз
    child, childctx = fixture()
    child.shared.judge_denials_in_a_row = 0  # у этого ребёнка судья разрешил
    apply_subagent_transfer(
        state, ctx, baseline, capture_subagent_transfer(child, childctx)
    )
    assert state.shared.judge_denials_in_a_row == 0


def _breaker_ctx(ctx, tripped):
    breaker = CircuitBreaker()
    for alias, reason in tripped.items():
        breaker.trip(alias, reason)
    ctx.model_router = SimpleNamespace(breaker=breaker)
    return breaker


def _breaker_transfer(child_tripped, parent_tripped):
    state, ctx = fixture()
    baseline = capture_subagent_transfer(state, ctx)
    child, childctx = fixture()
    _breaker_ctx(childctx, child_tripped)
    result = capture_subagent_transfer(child, childctx)
    breaker = _breaker_ctx(ctx, parent_tripped)
    apply_subagent_transfer(state, ctx, baseline, result)
    return breaker.snapshot()


def test_breaker_merge_last_reason_wins():
    assert _breaker_transfer({"a": "X"}, {"a": "Y"}) == {"a": "X"}


def test_breaker_merge_keeps_parent_only_keys():
    assert _breaker_transfer({"a": "X"}, {"b": "Z"}) == {"a": "X", "b": "Z"}
