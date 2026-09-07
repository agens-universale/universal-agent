"""I3/B: finish_gate в jsonl один раз, уже финальный; skipped → runtime_outcome."""
from __future__ import annotations

import json
from types import SimpleNamespace

from gc_core.intent_guard.guard import FinishGateDecision
from gc_core.intent_guard.turn_finish_emit import apply_finish_gate_to_turn_finish_output_in_state
from gc_core.intent_guard.validation_result import ValidationResult
from gc_core.message_policy.cycle_stop import append_cycle_stop_tool_outputs_for_batch
from gc_core.states import State
from gc_core.thread_store import make_message_key
from gc_core.tools.turn.submit_solution import SubmitSolution


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


def test_apply_finish_gate_skipped_sets_runtime_outcome():
    base = SubmitSolution.output_from_cycle_stop_arguments(
        json.dumps(
            {
                "solution_type": "direct_answer",
                "task_report": "ответ",
                "handoff": "{}",
            },
            ensure_ascii=False,
        )
    )
    patched = SubmitSolution.apply_finish_gate_decision(
        base, FinishGateDecision(outcome="skipped")
    )
    payload = json.loads(patched)
    assert payload["finish_gate"]["runtime_outcome"] == "skipped"


def test_finish_gate_persisted_once_after_apply_not_before():
    """cycle_stop кладёт output в state без flush; после apply — один persist с gate."""
    state = _make_state()
    arguments = json.dumps(
        {
            "solution_type": "direct_answer",
            "task_report": "готово",
            "handoff": "{}",
        },
        ensure_ascii=False,
    )
    call_id = "call_finish"
    persisted: list[list[dict]] = []
    known: set[str] = set()

    def persist(msgs: list[dict]) -> None:
        batch = []
        for m in msgs:
            key = make_message_key(m)
            if key in known:
                continue
            known.add(key)
            batch.append(dict(m))
        if batch:
            persisted.append(batch)

    append_cycle_stop_tool_outputs_for_batch(
        state=state,
        ctx=SimpleNamespace(config_dict={}, logger=None, tools_data={}),
        tool_calls=[
            {
                "type": "function_call",
                "name": "turn_finish",
                "arguments": arguments,
                "call_id": call_id,
                "id": "fc_finish",
            }
        ],
        run_agent_func=lambda *_a, **_k: None,
        persist_messages_callback=persist,
        backend_mode="chat_completions",
    )
    assert persisted == [], "finish output не должен попасть в jsonl до apply gate"

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
    ok = apply_finish_gate_to_turn_finish_output_in_state(
        state=state,
        finish_call_id=call_id,
        decision=FinishGateDecision(outcome="accept", validation=validation),
        logger=None,
        persist_messages_callback=persist,
    )
    assert ok is True
    assert len(persisted) == 1
    payload = json.loads(persisted[0][0]["output"])
    assert payload["finish_gate"]["action"] == "accept"
    # Повторный persist того же id — no-op (как known_message_keys).
    persist([state.messages[-1]])
    assert len(persisted) == 1
