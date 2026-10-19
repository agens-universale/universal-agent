"""Служебные сообщения о режиме plan: когда шлётся вход, когда выход."""

import threading

from gc_core import processing_responses as pr
from gc_core.agent_executor import AgentExecutor
from gc_core.execution.state import SharedStateData
from gc_core.message_policy.plan_messages import (
    PLAN_ENTER_MESSAGE,
    accepted_plan_messages,
    plan_accepted_message,
    plan_cancel_message,
    plan_resumed_message,
)
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
    wrap_technical_user_content,
)
from gc_core.security.permission_store import PermissionStore
from gc_core.tools.registry import get_builtin_tools_data
from gc_testkit.settings import fake_ctx, make_settings


def test_boundary_is_announced_once_per_change():
    from gc_core.message_policy.mode_messages import mode_boundary_messages

    assert mode_boundary_messages(
        current="plan", announced=None, can_propose_plan=True
    ) == (
        [PLAN_ENTER_MESSAGE],
        "plan",
    )
    assert mode_boundary_messages(
        current="plan", announced="plan", can_propose_plan=True
    ) == (
        [],
        "plan",
    )
    assert mode_boundary_messages(
        current="edit", announced="plan", can_propose_plan=True
    ) == (
        [plan_cancel_message("edit")],
        None,
    )
    assert mode_boundary_messages(
        current="edit", announced=None, can_propose_plan=True
    ) == ([], None)


def test_messages_name_the_tools_and_the_mode():
    assert "turn_propose_plan" in PLAN_ENTER_MESSAGE
    assert "turn_ask_human" in PLAN_ENTER_MESSAGE
    assert "'manual'" in plan_accepted_message("manual")


def test_approval_asks_to_check_what_the_plan_touches_first():
    """С составления плана проект мог измениться, а при повторном одобрении
    часть шагов уже сделана: перед исполнением модель сверяется с тем, что
    план затрагивает, — не со всем проектом."""
    assert plan_accepted_message("edit") == (
        "The plan is approved. Permission mode is now 'edit'. Before "
        "executing, check the current state of what the plan touches, then "
        "carry out the plan."
    )
    assert plan_resumed_message("edit") == (
        "The plan was approved earlier and may be partially done. Permission "
        "mode is now 'edit'. First check the current state of what the plan "
        "touches, then finish the remaining steps. If everything is already "
        "done, report that instead of redoing the work."
    )


def test_continuation_after_approval_is_one_text_for_every_frontend():
    """Исполнитель, CLI, ACP и TUI шлют одно и то же: служебное «план
    одобрен» (или «утверждён повторно») и уточнение человека вторым."""
    approved = wrap_technical_user_content(plan_accepted_message("auto"))
    assert accepted_plan_messages("auto") == [approved]
    assert accepted_plan_messages("auto", extra_text="без тестов") == [
        approved,
        "без тестов",
    ]
    assert accepted_plan_messages("auto", extra_text="  ") == [approved]
    assert accepted_plan_messages("edit", resumed=True) == [
        wrap_technical_user_content(plan_resumed_message("edit"))
    ]


def _executor(mode):
    from gc_core.states import State

    shared = SharedStateData(permission_mode=mode)
    ex = AgentExecutor.__new__(AgentExecutor)
    ex._lock = threading.Lock()
    ex._ctx = fake_ctx(
        permission_store=PermissionStore(shared.confirm_rules, shared=shared),
        tools_data=get_builtin_tools_data(make_settings()),
    )
    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=5,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.shared = shared
    return ex, state


def _told(ex, state):
    return [m["content"] for m in pr._mode_notices(state, ex._ctx)]


def test_agent_is_told_about_plan_once_and_the_mark_is_kept():
    ex, state = _executor("plan")
    assert _told(ex, state) == [
        wrap_technical_user_content(PLAN_ENTER_MESSAGE)
    ]
    assert state.shared.mode_announced == "plan"
    assert _told(ex, state) == []  # второй запрос молчит

    state.shared.permission_mode = "edit"
    assert _told(ex, state) == [
        wrap_technical_user_content(plan_cancel_message("edit"))
    ]
    assert state.shared.mode_announced is None


def test_other_modes_say_nothing():
    for mode in ("edit", "manual", None):
        ex, state = _executor(mode)
        assert _told(ex, state) == []


def test_removed_plan_is_announced_once_at_run_start():
    """После `/plan clear` план ещё виден модели в истории и в сводке."""
    from gc_core.message_policy.plan_messages import PLAN_REMOVED_MESSAGE

    ex, state = _executor("plan")
    state.shared.plan_removed_notice = True
    assert ex._plan_notices(state) == [PLAN_REMOVED_MESSAGE]
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
