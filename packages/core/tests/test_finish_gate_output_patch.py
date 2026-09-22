import json

from gc_core.intent_guard.guard import FinishGateDecision
from gc_core.intent_guard.turn_finish_emit import (
    apply_finish_gate_to_turn_finish_output_in_state,
    patch_function_call_output,
)
from gc_core.intent_guard.validation_result import ValidationResult
from gc_core.states import State
from gc_core.tools.turn.submit_solution import SubmitSolution
from gc_testkit import run_coro


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
    base = run_coro(
        SubmitSolution().acall(
            solution_type="full_solution",
            task_report="done",
            handoff="{}",
        )
    )
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
    assert "task_report" not in payload
    assert "Dialog turn finished" not in patched


async def test_apply_finish_gate_to_turn_finish_output_in_state_patches_message():
    state = _make_state()
    call_id = "call_finish"
    raw = await SubmitSolution().acall(
        solution_type="direct_answer",
        task_report="ответ",
        handoff="{}",
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
    ok = await apply_finish_gate_to_turn_finish_output_in_state(
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
            "output": '{"tool":"turn_finish","ok":true}',
        }
    ]
    updated = patch_function_call_output(messages, call_id="c1", new_output='{"tool":"turn_finish","ok":false}')
    assert updated is not None
    assert json.loads(messages[0]["output"])["ok"] is False
