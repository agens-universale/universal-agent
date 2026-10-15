"""Служебные сообщения модели о режиме: что и когда ей говорится."""

import threading
from types import SimpleNamespace

import pytest
from gc_testkit.settings import fake_ctx

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
    plan_cancel_message,
)
from gc_core.security import permission_mode
from gc_core.security.permission_mode import Mode
from gc_core.security.permission_store import PermissionStore


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
    assert mode_boundary_messages(current=current, announced=announced) == (
        messages,
        now_announced,
    )


def test_messages_name_the_mode_the_model_is_now_in():
    assert "'manual'" in auto_exit_message("manual")
    assert "'manual'" in plan_cancel_message("manual")
    # текст входа в auto не утверждает того, что станет ложью после выхода
    assert "not asked to confirm" not in AUTO_ENTER_MESSAGE
    assert "reviewed automatically" in AUTO_ENTER_MESSAGE


def test_mode_block_for_the_summary_carries_the_entry_text():
    """После сжатия истории сообщение о режиме уходит из контекста: правила
    действующего режима едут в сводке."""
    assert render_mode_block(None) == ""
    assert render_mode_block("edit") == ""
    auto = render_mode_block("auto")
    assert (
        auto.startswith('<permission_mode name="auto">')
        and AUTO_ENTER_MESSAGE in auto
    )
    assert PLAN_ENTER_MESSAGE in render_mode_block("plan")


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
        permission_store=PermissionStore(shared.confirm_rules, shared=shared)
    )
    return ex, SimpleNamespace(shared=shared)


def test_executor_announces_auto_once_and_says_when_it_is_left(monkeypatch):
    ex, state = _executor("auto", monkeypatch)
    assert ex._plan_notices(state) == [AUTO_ENTER_MESSAGE]
    assert state.shared.mode_announced == "auto"
    assert ex._plan_notices(state) == []
    state.shared.permission_mode = "manual"
    assert ex._plan_notices(state) == [auto_exit_message("manual")]
    assert state.shared.mode_announced is None


def test_plan_approved_with_auto_gets_the_auto_entry_and_no_cancel(
    monkeypatch,
):
    """Утверждение обнуляет объявление plan: «план не утверждён» слать нельзя,
    а вход в auto объявляется."""
    ex, state = _executor("auto", monkeypatch)
    state.shared.mode_announced = "plan"
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
    assert ex._plan_notices(state) == [AUTO_ENTER_MESSAGE]


def test_deciding_again_on_an_approved_plan_does_not_repeat_the_auto_entry(
    monkeypatch,
):
    ex, state = _executor("auto", monkeypatch)
    state.shared.mode_announced = "auto"
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
    assert ex._plan_notices(state) == []


def _compacted_summary(*, announced, parent_state_id=None, plan=None):
    """Тело сводки после настоящего сжатия истории."""
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
    state.shared = SharedStateData(mode_announced=announced, plan=plan)
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
    )


def test_compaction_keeps_the_rules_of_the_mode_for_the_root_agent_only():
    """Сжатие уносит сообщение о входе в режим. Корневому агенту правила
    возвращает сводка; субагент объявлений о режиме не получает вовсе."""
    assert AUTO_ENTER_MESSAGE in _compacted_summary(announced="auto")
    assert PLAN_ENTER_MESSAGE in _compacted_summary(announced="plan")
    assert "<permission_mode" not in _compacted_summary(announced=None)
    assert "<permission_mode" not in _compacted_summary(
        announced="auto", parent_state_id="root"
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
