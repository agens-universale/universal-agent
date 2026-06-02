import json

from intent_guard.guard import FinishGateDecision
from intent_guard.submit_solution_emit import (
    apply_finish_gate_to_submit_output_in_state,
    patch_function_call_output,
)
from intent_guard.validation_result import ValidationResult
from states import State
from tools.session.submit_solution import SubmitSolution


def _make_state() -> State:
    return State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=2,
        system_prompt_id="v1",
        use_server_subagents=False,
    )


def test_apply_finish_gate_decision_reject_marks_output_not_ok():
    base = SubmitSolution.output_from_cycle_stop_arguments(
        json.dumps(
            {
                "solution_type": "full_solution",
                "task_report": "done",
                "handoff": "{}",
            },
            ensure_ascii=False,
        )
    )
    assert base is not None
    validation = ValidationResult(
        ok=False,
        action="fix_report",
        trace_ok=True,
        report_ok=False,
        trace_gaps=[],
        report_mismatches=["mismatch"],
        missing_requirements=[],
        conflicts=[],
        coverage_score=0.5,
        feedback_for_agent="",
    )
    decision = FinishGateDecision(
        outcome="reject",
        validation=validation,
        feedback_text="Finish gate: fix_report.\nИсправь submit_solution.",
    )
    patched = SubmitSolution.apply_finish_gate_decision(base, decision)
    payload = json.loads(patched)
    assert payload["ok"] is False
    assert payload["status"] == "rejected_by_finish_gate"
    assert "Finish gate: fix_report" in payload["message"]
    assert payload["finish_gate"]["action"] == "fix_report"
    assert payload["task_report"] == "done"
    assert "Dialog turn finished" not in patched


def test_apply_finish_gate_to_submit_output_in_state_patches_message():
    state = _make_state()
    call_id = "call_finish"
    raw = SubmitSolution.output_from_cycle_stop_arguments(
        json.dumps(
            {
                "solution_type": "direct_answer",
                "task_report": "ответ",
                "handoff": "{}",
            },
            ensure_ascii=False,
        )
    )
    state.messages = [
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": raw,
            "id": "fco_1",
        }
    ]
    validation = ValidationResult(
        ok=True,
        action="accept",
        trace_ok=True,
        report_ok=True,
        trace_gaps=[],
        report_mismatches=[],
        missing_requirements=[],
        conflicts=[],
        coverage_score=1.0,
        feedback_for_agent="",
    )
    decision = FinishGateDecision(outcome="accept", validation=validation)
    ok = apply_finish_gate_to_submit_output_in_state(
        state=state,
        finish_call_id=call_id,
        decision=decision,
        logger=None,
    )
    assert ok is True
    payload = json.loads(state.messages[0]["output"])
    assert payload["ok"] is True
    assert payload["finish_gate"]["action"] == "accept"


def test_patch_function_call_output_returns_updated_message():
    messages = [
        {
            "type": "function_call_output",
            "call_id": "c1",
            "output": '{"tool":"submit_solution","ok":true}',
        }
    ]
    updated = patch_function_call_output(
        messages, call_id="c1", new_output='{"tool":"submit_solution","ok":false}'
    )
    assert updated is not None
    assert json.loads(messages[0]["output"])["ok"] is False
