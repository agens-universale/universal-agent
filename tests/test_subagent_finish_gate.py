"""Finish gate для subagent: assignment в scope, не CLI user оркестратора."""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import intent_guard.guard as intent_guard_guard

from intent_guard.guard import FinishGateGuard
from intent_guard.validation_result import ValidationResult
from intent_log.user_turn import init_subagent_open_turn
from persistence.state_storage import save_state
from states import SharedStateData, State
from tools.subagents.runner import SubagentRunner


def _make_orchestrator_state():
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "read_file"}],
        tools_descr=["read desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt="System\n<specific_role>x</specific_role>",
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
    state.current_user_intent = ["создай приложение для бухгалтерии"]
    state.user_intent_log = []
    save_state(state)
    return state


def _make_config():
    return {
        "use_local_subagents": True,
        "backend_api_mode": "chat_completions",
        "strict_submit_solution_validation": True,
        "finish_gate_max_iterations": 10,
        "subagent_finish_gate_max_iterations": 3,
        "enabled_tools": [],
        "skills_data_path": "",
        "mcp_servers": [],
        "model": "test-model",
        "system_prompt_id": "test-prompt",
    }


def _make_ctx(cfg):
    return SimpleNamespace(
        config_dict=cfg,
        auth_connector=None,
        logger=logging.getLogger("test_subagent_finish_gate"),
    )


def _user_msg(content: str) -> dict:
    return {"role": "user", "content": content, "id": "m_user_sub"}


def test_subagent_gate_validates_assignment_not_orchestrator_cli_intent():
    orch = _make_orchestrator_state()
    cfg = _make_config()
    runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
    child = runner._build_child_state("worker")
    assignment = "измени foo.py"
    user_message = _user_msg(assignment)
    runner._build_initial_messages(child, "worker", user_message)
    init_subagent_open_turn(child, assignment)

    captured: dict[str, object] = {}

    def _continue_work(**kwargs):
        captured["current_user_requests"] = list(kwargs.get("current_user_requests") or [])
        return ValidationResult(
            ok=False,
            action="continue_work",
            trace_ok=False,
            report_ok=True,
            trace_gaps=["нет правок в foo.py"],
            report_mismatches=[],
            missing_requirements=["нет правок в foo.py"],
            conflicts=[],
            coverage_score=0.0,
            feedback_for_agent="сделай bar.py вместо foo",
        )

    with patch.object(
        intent_guard_guard,
        "validate_final_report_vs_user_intent",
        _continue_work,
    ):
        guard = FinishGateGuard(ctx=_make_ctx(cfg), state=child)
        decision = guard.evaluate_submit_solution(
            submit_solution_arguments={"solution_type": "full_solution", "task_report": "bar"},
        )

    assert decision.outcome == "reject"
    assert decision.validation is not None
    assert decision.validation.action == "continue_work"
    from intent_fixtures import intent_user_ask_texts_from_jsonl

    requests = intent_user_ask_texts_from_jsonl(captured["current_user_requests"])
    assert requests == [assignment]
    assert "бухгалтерии" not in " ".join(captured["current_user_requests"])
    assert "continue_work" in decision.feedback_text.lower()
    assert "foo.py" in decision.feedback_text


def test_subagent_seed_keeps_orchestrator_cli_scope_unchanged():
    """Как SubagentRunner.run: assignment только у child, CLI intent — у orch."""
    orch = _make_orchestrator_state()
    cfg = _make_config()
    runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
    child = runner._build_child_state("worker")
    task_text = "добавь тесты в foo.py"
    user_message = _user_msg(task_text)
    runner._build_initial_messages(child, "worker", user_message)
    init_subagent_open_turn(child, task_text)

    from intent_log import ACTION_USER_ASK

    assert len(child.current_user_intent) == 1
    assert child.current_user_intent[0].action == ACTION_USER_ASK
    assert child.current_user_intent[0].content == task_text
    assert child.parent_state_id == orch.state_id
    assert orch.current_user_intent == ["создай приложение для бухгалтерии"]
    assert orch.user_intent_log == []
