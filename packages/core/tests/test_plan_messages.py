"""Служебные сообщения о режиме plan: когда шлётся вход, когда выход."""

import threading
from types import SimpleNamespace

from gc_core.agent_executor import AgentExecutor
from gc_core.execution.state import SharedStateData
from gc_core.message_policy.plan_messages import (
    plan_cancel_message,
    PLAN_ENTER_MESSAGE,
    plan_accepted_message,
)
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)
from gc_core.security.permission_store import PermissionStore
from gc_testkit.settings import fake_ctx


def test_boundary_is_announced_once_per_change():
    from gc_core.message_policy.mode_messages import mode_boundary_messages

    assert mode_boundary_messages(current="plan", announced=None) == (
        [PLAN_ENTER_MESSAGE],
        "plan",
    )
    assert mode_boundary_messages(current="plan", announced="plan") == (
        [],
        "plan",
    )
    assert mode_boundary_messages(current="edit", announced="plan") == (
        [plan_cancel_message("edit")],
        None,
    )
    assert mode_boundary_messages(current="edit", announced=None) == ([], None)


def test_messages_name_the_tools_and_the_mode():
    assert "turn_propose_plan" in PLAN_ENTER_MESSAGE
    assert "turn_ask_human" in PLAN_ENTER_MESSAGE
    assert "'manual'" in plan_accepted_message("manual")


def _executor(mode):
    shared = SharedStateData(permission_mode=mode)
    ex = AgentExecutor.__new__(AgentExecutor)
    ex._lock = threading.Lock()
    ex._ctx = fake_ctx(
        permission_store=PermissionStore(shared.confirm_rules, shared=shared)
    )
    return ex, SimpleNamespace(shared=shared)


def test_executor_announces_plan_at_run_start_and_remembers_it():
    ex, state = _executor("plan")
    assert ex._plan_notices(state) == [PLAN_ENTER_MESSAGE]
    assert state.shared.mode_announced == "plan"
    assert ex._plan_notices(state) == []  # второй прогон молчит

    state.shared.permission_mode = "edit"
    assert ex._plan_notices(state) == [plan_cancel_message("edit")]
    assert state.shared.mode_announced is None


def test_other_modes_say_nothing():
    for mode in ("edit", "manual", None):
        ex, state = _executor(mode)
        assert ex._plan_notices(state) == []


def test_removed_plan_is_announced_once_and_before_the_mode_boundary():
    """После `/plan clear` план ещё виден модели в истории и в сводке."""
    from gc_core.message_policy.plan_messages import PLAN_REMOVED_MESSAGE

    ex, state = _executor("plan")
    state.shared.plan_removed_notice = True
    assert ex._plan_notices(state) == [
        PLAN_REMOVED_MESSAGE,
        PLAN_ENTER_MESSAGE,
    ]
    assert state.shared.plan_removed_notice is False
    assert ex._plan_notices(state) == []
    assert "<current_plan>" in PLAN_REMOVED_MESSAGE


def test_notices_are_technical_messages():
    """Служебное: в журнал запросов человека не попадает."""
    from gc_core.message_policy.user_message_policy import (
        wrap_technical_user_content,
    )

    message = {
        "role": "user",
        "content": wrap_technical_user_content(PLAN_ENTER_MESSAGE),
    }
    assert not extract_real_user_message_text(message)
