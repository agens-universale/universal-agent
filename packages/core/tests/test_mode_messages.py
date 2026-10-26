"""Служебные сообщения модели о режиме: что и когда ей говорится."""

import threading

import pytest
from gc_core import processing_responses as pr
from gc_core.agent_executor import AgentExecutor
from gc_core.execution.state import SharedStateData
from gc_core.message_policy.mode_messages import (
    AUTO_ENTER_MESSAGE,
    auto_exit_message,
    mode_boundary_messages,
    render_mode_block,
)
from gc_core.message_policy.plan_messages import (
    PLAN_ENTER_MESSAGE,
    PLAN_RESEARCH_ENTER_MESSAGE,
    plan_cancel_message,
    plan_research_exit_message,
)
from gc_core.message_policy.user_message_policy import (
    wrap_technical_user_content,
)
from gc_core.security import permission_mode
from gc_core.security.permission_mode import Mode
from gc_core.security.permission_store import PermissionStore
from gc_core.tools.registry import get_builtin_tools_data
from gc_testkit.settings import fake_ctx, make_settings


@pytest.mark.parametrize(
    "announced, current, messages, now_announced",
    [
        # вход
        (None, "plan", [PLAN_ENTER_MESSAGE], "plan"),
        (None, "auto", [AUTO_ENTER_MESSAGE], "auto"),
        # уже объявлено — молчим
        ("plan", "plan", [], "plan"),
        ("auto", "auto", [], "auto"),
        # режимы без правил для модели
        (None, "edit", [], None),
        (None, "manual", [], None),
        # выход
        ("plan", "edit", [plan_cancel_message("edit")], None),
        ("auto", "manual", [auto_exit_message("manual")], None),
        # между режимами с правилами
        ("auto", "plan", [PLAN_ENTER_MESSAGE], "plan"),
        (
            "plan",
            "auto",
            [plan_cancel_message("auto"), AUTO_ENTER_MESSAGE],
            "auto",
        ),
    ],
)
def test_one_rule_for_every_transition(
    announced, current, messages, now_announced
):
    assert mode_boundary_messages(
        current=current, announced=announced, can_propose_plan=True
    ) == (messages, now_announced)


@pytest.mark.parametrize(
    "announced, current, messages, now_announced",
    [
        (None, "plan", [PLAN_RESEARCH_ENTER_MESSAGE], "plan"),
        ("plan", "plan", [], "plan"),
        ("plan", "edit", [plan_research_exit_message("edit")], None),
        (
            "plan",
            "auto",
            [plan_research_exit_message("auto"), AUTO_ENTER_MESSAGE],
            "auto",
        ),
        (None, "auto", [AUTO_ENTER_MESSAGE], "auto"),
    ],
)
def test_agent_without_the_plan_tool_returns_findings_as_text(
    announced, current, messages, now_announced
):
    assert mode_boundary_messages(
        current=current, announced=announced, can_propose_plan=False
    ) == (messages, now_announced)


def test_texts_without_the_plan_tool_do_not_promise_a_plan():
    assert "turn_propose_plan" not in PLAN_RESEARCH_ENTER_MESSAGE
    assert "return your findings as text" in PLAN_RESEARCH_ENTER_MESSAGE
    assert "not approved" not in plan_research_exit_message("edit")
    assert "'edit'" in plan_research_exit_message("edit")


def test_mode_block_for_the_summary_carries_the_entry_text():
    """После сжатия истории сообщение о режиме уходит из контекста: правила
    действующего режима едут в сводке."""
    assert render_mode_block(None, can_propose_plan=True) == ""
    assert render_mode_block("edit", can_propose_plan=True) == ""
    auto = render_mode_block("auto", can_propose_plan=True)
    assert (
        auto.startswith('<permission_mode name="auto">')
        and AUTO_ENTER_MESSAGE in auto
    )
    assert PLAN_ENTER_MESSAGE in render_mode_block(
        "plan", can_propose_plan=True
    )
    assert PLAN_RESEARCH_ENTER_MESSAGE in render_mode_block(
        "plan", can_propose_plan=False
    )


def test_messages_name_the_mode_the_model_is_now_in():
    assert "'manual'" in auto_exit_message("manual")
    assert "'manual'" in plan_cancel_message("manual")
    # текст входа в auto не утверждает того, что станет ложью после выхода
    assert "not asked to confirm" not in AUTO_ENTER_MESSAGE
    assert "reviewed automatically" in AUTO_ENTER_MESSAGE


def _root_state(shared):
    from gc_core.states import State

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
    return state


def _executor(mode, monkeypatch):
    monkeypatch.setattr(
        permission_mode,
        "AVAILABLE_MODES",
        (Mode.EDIT, Mode.MANUAL, Mode.PLAN, Mode.AUTO),
    )
    shared = SharedStateData(permission_mode=mode)
    ex = AgentExecutor.__new__(AgentExecutor)
    ex._lock = threading.Lock()
    ex._ctx = fake_ctx(
        permission_store=PermissionStore(shared.confirm_rules, shared=shared),
        tools_data=get_builtin_tools_data(make_settings()),
    )
    return ex, _root_state(shared)


def _told(ex, state):
    """Что агенту скажут о режиме перед ближайшим запросом к модели."""
    return [m["content"] for m in pr._mode_notices(state, ex._ctx)]


def test_agent_is_told_about_auto_once_and_when_it_is_left(monkeypatch):
    ex, state = _executor("auto", monkeypatch)
    assert _told(ex, state) == [
        wrap_technical_user_content(AUTO_ENTER_MESSAGE)
    ]
    assert state.shared.mode_announced == "auto"
    assert _told(ex, state) == []
    state.shared.permission_mode = "manual"
    assert _told(ex, state) == [
        wrap_technical_user_content(auto_exit_message("manual"))
    ]
    assert state.shared.mode_announced is None


def test_plan_approved_with_auto_gets_the_auto_entry_and_no_cancel(
    monkeypatch,
):
    """Утверждение обнуляет объявление plan: «план не утверждён» слать нельзя,
    а вход в auto объявляется."""
    ex, state = _executor("auto", monkeypatch)
    state.mode_announced = "plan"
    state.shared.plan = {
        "text": "1.",
        "status": "proposed",
        "approved_mode": None,
    }
    state.thread_id = "t1"
    ex._plan_transition_pending = None
    ex._plan_rework_thread = None
    ex.accept_plan("auto", "t1")
    ex._apply_plan_changes(state, [])
    assert _told(ex, state) == [
        wrap_technical_user_content(AUTO_ENTER_MESSAGE)
    ]


def test_deciding_again_on_an_approved_plan_does_not_repeat_the_auto_entry(
    monkeypatch,
):
    ex, state = _executor("auto", monkeypatch)
    state.mode_announced = "auto"
    state.shared.plan = {
        "text": "1.",
        "status": "approved",
        "approved_mode": "auto",
    }
    state.thread_id = "t1"
    ex._plan_transition_pending = None
    ex._plan_rework_thread = None
    ex.accept_plan("auto", "t1")
    ex._apply_plan_changes(state, [])
    assert _told(ex, state) == []


def _compacted_summary(
    *,
    announced,
    parent_state_id=None,
    plan=None,
    root_mark=None,
    can_propose_plan=True,
):
    """Тело сводки после настоящего сжатия истории. ``announced`` — отметка
    этого агента, ``root_mark`` — отметка корня в общем состоянии."""
    from gc_core.states import State
    from gc_core.summarize.compaction import apply_summary_state_patch

    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=5,
        system_prompt_id="v1",
        use_server_subagents=False,
        parent_state_id=parent_state_id,
    )
    state.shared = SharedStateData(mode_announced=root_mark, plan=plan)
    state.mode_announced = announced
    state.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    return apply_summary_state_patch(
        state=state,
        raw_summary="итог",
        last_dialog_steps="",
        summarized_messages_count=2,
        intent_log_preview_chars=1000,
        can_propose_plan=can_propose_plan,
    )


def test_compaction_keeps_the_rules_of_the_mode_the_agent_was_told():
    """Сжатие уносит сообщение о входе в режим — правила возвращает сводка.
    Отметка у каждого агента своя: субагенту — его объявление, не корня."""
    assert AUTO_ENTER_MESSAGE in _compacted_summary(announced="auto")
    assert PLAN_ENTER_MESSAGE in _compacted_summary(announced="plan")
    assert "<permission_mode" not in _compacted_summary(announced=None)
    assert AUTO_ENTER_MESSAGE in _compacted_summary(
        announced="auto", parent_state_id="root"
    )
    assert "<permission_mode" not in _compacted_summary(
        announced=None, parent_state_id="root", root_mark="auto"
    )
    assert PLAN_RESEARCH_ENTER_MESSAGE in _compacted_summary(
        announced="plan", parent_state_id="root", can_propose_plan=False
    )


def test_compaction_keeps_the_session_plan_for_the_root_agent_only():
    """План — дело корневого агента: субагенту тул плана не дают, и сводка
    сжатия его истории не должна приносить план сессии из общего состояния."""
    from gc_core.plan_store import new_plan_block

    plan = new_plan_block("1. шаг плана сессии")
    assert "шаг плана сессии" in _compacted_summary(
        announced="plan", plan=plan
    )
    child = _compacted_summary(
        announced="plan", parent_state_id="root", plan=plan
    )
    assert "<current_plan" not in child
    assert "шаг плана сессии" not in child
