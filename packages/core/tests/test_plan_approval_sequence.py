"""Одобрение плана — одна последовательность ядра для фронтендов."""

import threading

import pytest
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx

from gc_core.agent_executor import AgentExecutor
from gc_core.execution.state import SharedStateData
from gc_core.message_policy.plan_messages import plan_accepted_message
from gc_core.message_policy.user_message_policy import (
    wrap_technical_user_content,
)
from gc_core.plan_store import new_plan_block, plan_is_accepted
from gc_core.security.permission_store import PermissionStore
from gc_core.states import State


def _executor(shared=None):
    """Исполнитель без прогона: прогон и учёт записываются, не исполняются."""
    shared = shared or SharedStateData()
    ex = AgentExecutor.__new__(AgentExecutor)
    ex._lock = threading.Lock()
    ex._state = None
    ex._permission_mode_choice = None
    ex._plan_transition_pending = None
    ex._plan_rework_thread = None
    ex._ctx = fake_ctx(
        permission_store=PermissionStore(shared.confirm_rules, shared=shared)
    )
    events = []

    async def aclose_run_accounting(*, console=True):
        events.append(("close", ex._permission_mode_choice))

    async def arun(messages, **kwargs):
        events.append(("run", messages, kwargs))
        return "исполнено"

    ex.aclose_run_accounting = aclose_run_accounting
    ex.arun = arun
    return ex, events


def _approved(mode):
    return wrap_technical_user_content(plan_accepted_message(mode))


@pytest.mark.parametrize("mode", ["edit", "auto", "manual"])
def test_accepted_plan_runs_in_the_chosen_mode(mode):
    ex, events = _executor()
    assert run_coro(ex.aexecute_accepted_plan(mode, "t1")) == "исполнено"
    assert ex._permission_mode_choice == mode
    assert ex._plan_transition_pending == ("approved", mode, "t1")
    run_kwargs = {
        "thread_id": "t1",
        "system": None,
        "verbose": None,
        "account_run": True,
    }
    assert events == [
        ("close", None),  # учёт прошлого прогона — до смены режима
        ("run", [_approved(mode)], run_kwargs),
    ]


def test_run_options_reach_the_run():
    ex, events = _executor()
    run_coro(
        ex.aexecute_accepted_plan(
            "edit", "t1", verbose=False, account_run=False, system="acp"
        )
    )
    assert events[-1][2] == {
        "thread_id": "t1",
        "system": "acp",
        "verbose": False,
        "account_run": False,
    }


def test_user_remark_follows_the_approval_and_blank_adds_nothing():
    ex, _ = _executor()
    assert ex.prepare_accepted_plan("edit", "t1", extra_text="без тестов") == [
        _approved("edit"),
        "без тестов",
    ]
    assert ex.prepare_accepted_plan("edit", "t1", extra_text="  ") == [
        _approved("edit")
    ]


def test_mode_spelling_is_normalized_before_it_is_stored_and_announced():
    ex, _ = _executor()
    assert ex.prepare_accepted_plan("Auto", "t1") == [_approved("auto")]
    assert ex._permission_mode_choice == "auto"
    assert ex._plan_transition_pending == ("approved", "auto", "t1")


@pytest.mark.parametrize("mode", ["plan", "турбо"])
def test_not_an_execution_mode_is_refused_before_anything_is_armed(mode):
    """Негодный режим — отказ до любых действий: учёт прошлого прогона
    тоже не закрывается."""
    ex, events = _executor()
    with pytest.raises(ValueError):
        run_coro(ex.aexecute_accepted_plan(mode, "t1"))
    assert ex._permission_mode_choice is None
    assert ex._plan_transition_pending is None
    assert events == []


def test_after_approval_the_judge_sees_the_plan_as_approved():
    """Судья действий берёт одобренный план по `plan_is_accepted` — с
    начала прогона продолжения план одобрен в выбранном режиме."""
    shared = SharedStateData(
        permission_mode="plan", plan=new_plan_block("1. шаг")
    )
    ex, _ = _executor(shared)
    ex.prepare_accepted_plan("auto", "t1")
    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=5,
        system_prompt_id="v1",
        use_server_subagents=False,
        thread_id="t1",
    )
    state.shared = shared
    ex._apply_plan_changes(state, [])
    assert plan_is_accepted(state.shared.plan)
    assert state.shared.plan["approved_mode"] == "auto"
