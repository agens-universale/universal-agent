"""Переходы статуса плана между прогонами: одобрение и доработка."""

import threading
from types import SimpleNamespace

from gc_core.agent_executor import AgentExecutor
from gc_core.execution.state import SharedStateData
from gc_core.message_policy.user_message_policy import (
    wrap_technical_user_content,
)
from gc_core.plan_store import new_plan_block
from gc_core.security.permission_store import PermissionStore
from gc_testkit.settings import fake_ctx


def _executor(mode="plan", *, thread_id="t1", plan=True):
    shared = SharedStateData(
        permission_mode=mode, plan=new_plan_block("1. шаг") if plan else None
    )
    ex = AgentExecutor.__new__(AgentExecutor)
    ex._lock = threading.Lock()
    ex._plan_transition_pending = None
    ex._plan_rework_thread = None
    ex._ctx = fake_ctx(
        permission_store=PermissionStore(shared.confirm_rules, shared=shared)
    )
    state = SimpleNamespace(shared=shared, thread_id=thread_id)
    ex._state = state
    return ex, state


def _user(text="доработай"):
    return [{"role": "user", "content": text}]


def _technical():
    return [
        {"role": "user", "content": wrap_technical_user_content("approved")}
    ]


def test_approval_is_applied_at_the_start_of_the_next_run():
    ex, state = _executor()
    state.shared.mode_announced = "plan"
    ex.accept_plan("edit", "t1")
    assert state.shared.plan["status"] == "proposed"  # до прогона — не тронут
    ex._apply_plan_changes(state, _technical())
    assert state.shared.plan["status"] == "approved"
    assert state.shared.plan["approved_mode"] == "edit"
    # выход из plan через одобрение: «план не одобрен» модели не шлётся
    assert state.shared.mode_announced is None


def test_approval_never_lands_on_another_thread_and_does_not_linger():
    ex, state = _executor(thread_id="другой")
    ex.accept_plan("edit", "t1")
    ex._apply_plan_changes(state, _technical())
    assert state.shared.plan["status"] == "proposed"
    assert ex._plan_transition_pending is None  # погашен чужим прогоном


def test_rework_needs_the_button_and_a_real_message():
    ex, state = _executor()
    ex._apply_plan_changes(state, _user())  # сообщение без кнопки
    assert state.shared.plan["status"] == "proposed"

    ex.arm_plan_rework("t1")
    ex._apply_plan_changes(
        state, _technical()
    )  # кнопка, но не сообщение человека
    assert state.shared.plan["status"] == "proposed"
    ex._apply_plan_changes(state, _user())  # признак уже погашен
    assert state.shared.plan["status"] == "proposed"

    ex.arm_plan_rework("t1")
    ex._apply_plan_changes(state, _user())
    assert state.shared.plan["status"] == "reworking"
    assert state.shared.plan["approved_mode"] is None


def test_rework_of_an_approved_plan_drops_the_approved_mode():
    ex, state = _executor()
    state.shared.plan.update(status="approved", approved_mode="edit")
    ex.arm_plan_rework("t1")
    ex._apply_plan_changes(state, _user())
    assert state.shared.plan["status"] == "reworking"
    assert state.shared.plan["approved_mode"] is None


def test_rework_outside_plan_mode_or_in_another_thread_changes_nothing():
    ex, state = _executor(mode="edit")
    ex.arm_plan_rework("t1")
    ex._apply_plan_changes(state, _user())
    assert state.shared.plan["status"] == "proposed"
    ex, state = _executor(thread_id="другой")
    ex.arm_plan_rework("t1")
    ex._apply_plan_changes(state, _user())
    assert state.shared.plan["status"] == "proposed"


def test_snapshot_is_only_for_the_expected_thread():
    ex, state = _executor()
    assert ex.plan_snapshot("t1")["text"] == "1. шаг"
    assert ex.plan_snapshot("другой") is None
    assert ex.plan_snapshot(None) is None
    ex, _state = _executor(plan=False)
    assert ex.plan_snapshot("t1") is None


def test_clearing_the_plan_disarms_pending_decisions_of_that_thread_only():
    ex, _state = _executor()
    ex.accept_plan("edit", "t1")
    ex.arm_plan_rework("t1")
    ex.clear_plan("другой")
    assert ex._plan_transition_pending is not None
    assert ex._plan_rework_thread == "t1"
    ex.clear_plan("t1")
    assert ex._plan_transition_pending is None
    assert ex._plan_rework_thread is None
