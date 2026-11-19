import json
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from gc_core.intent_guard.guard import FinishGateGuard
from gc_core.intent_guard.trace_digest import FinishGateTraceDigest
from gc_core.intent_guard.turn_finish_emit import (
    apply_finish_gate_to_turn_finish_output_in_state,
)
from gc_core.tools.turn.submit_solution import SubmitSolution
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx


def _verdict(*, accept: bool) -> str:
    return json.dumps(
        {
            "action": "accept" if accept else "fix_report_and_continue",
            "trace_ok": accept,
            "report_ok": accept,
            "is_question": False,
            "trace_gaps": [] if accept else ["missing test"],
            "report_mismatches": [] if accept else ["unsupported report"],
        }
    )


_YES = _verdict(accept=True)
_NO = _verdict(accept=False)
_SINGLE_QUOTED_NO = _NO.replace('"', "'")
_SINGLE_QUOTED_KEYS_NO = re.sub(r'"([a-z_]+)":', r"'\1':", _NO)
_JSON_FENCE_NO = f"```json\n{_NO}\n```"


@pytest.mark.parametrize(
    ("first", "retry", "calls", "status", "findings"),
    [
        (
            f"Passing example: {_YES}\nActual: {_SINGLE_QUOTED_NO}",
            "No verdict",
            2,
            "rejected_by_finish_gate",
            False,
        ),
        (
            f"Actual: {_SINGLE_QUOTED_NO}\nPassing example: {_YES}",
            "No verdict",
            2,
            "rejected_by_finish_gate",
            False,
        ),
        (
            f"Final:\n{_NO}\nPassing example: {_YES.replace(chr(34), chr(39))}",
            "No verdict",
            2,
            "rejected_by_finish_gate",
            True,
        ),
        (
            f"Passing example: {_YES.replace(chr(34), chr(39))}\nFinal:\n{_YES}",
            "No verdict",
            2,
            "submitted",
            False,
        ),
        (
            f"Final:\n{_NO}\n```log\n</think>\n```",
            "No verdict",
            1,
            "rejected_by_finish_gate",
            True,
        ),
        (
            f"{_JSON_FENCE_NO}\n```text\n</think>\n```",
            "No verdict",
            1,
            "rejected_by_finish_gate",
            True,
        ),
        (
            f"Final:\n{_NO}\nThe log printed `{{` literally.",
            "No verdict",
            1,
            "rejected_by_finish_gate",
            True,
        ),
        (
            f"Passing example: {_YES}\nActual: {_NO[:-1]}",
            "No verdict",
            2,
            "rejected_by_finish_gate",
            False,
        ),
        (
            f"Code sample: `{_YES}`\nFinal:\n{_NO}",
            "No verdict",
            1,
            "rejected_by_finish_gate",
            True,
        ),
        (
            f"```text\n{_YES}\n```\nFinal:\n{_NO}",
            "No verdict",
            2,
            "rejected_by_finish_gate",
            True,
        ),
        (
            f"```text\n{_NO}\n```",
            "No verdict",
            1,
            "rejected_by_finish_gate",
            True,
        ),
        (
            f"Final:\n{_YES}\nCode sample: `{_NO}`",
            "No verdict",
            1,
            "submitted",
            False,
        ),
        (
            f"Final:\n{_YES}\nThe config was {{'action': 'append'}}.",
            "No verdict",
            1,
            "submitted",
            False,
        ),
        ("No verdict", "Still no verdict", 2, "submitted", False),
        (
            f"```jsonc\n{_NO}\n```",
            "No verdict",
            1,
            "rejected_by_finish_gate",
            True,
        ),
        (
            f"{_JSON_FENCE_NO}\n```json\n{_YES}\n```",
            f"{_JSON_FENCE_NO}\n```json\n{_YES}\n```",
            2,
            "rejected_by_finish_gate",
            True,
        ),
    ],
)
def test_finish_gate_review_safety(first, retry, calls, status, findings):
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "agent.strict_turn_finish_validation": True,
        },
        logger=None,
        auxiliary_llm=SimpleNamespace(
            max_tokens=8192, judge_max_tokens=10000, completion_kwargs={}
        ),
    )
    raw_output = run_coro(
        SubmitSolution().acall(
            solution_type="direct_answer", task_report="done", handoff="{}"
        )
    )
    state = SimpleNamespace(
        messages=[
            {
                "type": "function_call_output",
                "call_id": "finish",
                "output": raw_output,
            }
        ],
        parent_state_id=None,
        current_user_intent=[],
        user_intent_log=[],
        finish_gate_rejection_count=0,
    )
    digest = FinishGateTraceDigest(
        trace_digest_text="saved trace", digest_chars=11
    )
    with (
        patch(
            "gc_core.summarize.llm.arequest_chat_completion_text",
            side_effect=[first, retry],
        ) as model,
        patch(
            "gc_core.intent_guard.guard.build_finish_gate_trace_digest",
            return_value=digest,
        ),
        patch(
            "gc_core.intent_guard.final_report_consistency.build_finish_gate_trace_digest",
            return_value=digest,
        ),
    ):
        decision = run_coro(
            FinishGateGuard(ctx=ctx, state=state).aevaluate_turn_finish(
                finish_handoff_arguments={"solution_type": "direct_answer"}
            )
        )
    assert model.call_count == calls
    assert run_coro(
        apply_finish_gate_to_turn_finish_output_in_state(
            state=state, finish_call_id="finish", decision=decision
        )
    )
    output = json.loads(state.messages[0]["output"])
    assert output["status"] == status
    gate = output["finish_gate"]
    expected_gaps = ["missing test"] if findings else []
    expected_mismatches = ["unsupported report"] if findings else []
    assert gate["trace_gaps"] == expected_gaps
    assert gate["report_mismatches"] == expected_mismatches


@pytest.mark.parametrize(
    ("first", "retry", "calls", "gaps", "mismatches"),
    [
        (
            f"Example: {_YES}\nActual:\n```text\n{_SINGLE_QUOTED_KEYS_NO}\n```",
            _NO,
            2,
            ["missing test"],
            ["unsupported report"],
        ),
        (
            f"Example: {_YES}\nActual:\n```text\n{_NO[:-1]},}}\n```",
            _NO,
            2,
            ["missing test"],
            ["unsupported report"],
        ),
        (
            _NO
            + "\n"
            + _NO.replace("missing test", "missing check").replace(
                "unsupported report", "unsupported claim"
            ),
            "No verdict",
            2,
            ["missing test", "missing check"],
            ["unsupported report", "unsupported claim"],
        ),
        (
            f"```json\n{_NO}\n```\n```json\n{_NO.replace('missing test', 'missing check').replace('unsupported report', 'unsupported claim')}\n```",
            "No verdict",
            2,
            ["missing test", "missing check"],
            ["unsupported report", "unsupported claim"],
        ),
        (
            f'{_NO}\n```text\nDebug metadata mentions "trace_ok": false in a log.\n```',
            "No verdict",
            1,
            ["missing test"],
            ["unsupported report"],
        ),
        (
            f"```json\n{_YES}\n```\nActual verdict:\n```text\n{_NO[:-1]}",
            _NO,
            2,
            ["missing test"],
            ["unsupported report"],
        ),
        *[
            (
                prefix
                + (
                    _NO.replace("missing test", "expected literal `")
                    if suffix.startswith(" Log")
                    else _NO
                )
                + suffix,
                "No verdict",
                1,
                ["expected literal `"]
                if suffix.startswith(" Log")
                else ["missing test"],
                ["unsupported report"],
            )
            for prefix in ("", "Final verdict follows.\n")
            for suffix in (
                "\nLog output:\n```text\nINFO runner stopped",
                "\nLog output:\n~~~text\n</think>\n~~~",
                "\nLog output:\n````text\n</think>\n````",
                " Log contains `command`.",
            )
        ],
    ],
)
def test_neighbor_verdicts_keep_findings(
    first, retry, calls, gaps, mismatches
):
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "agent.strict_turn_finish_validation": True,
        },
        logger=None,
        auxiliary_llm=SimpleNamespace(max_tokens=8192, judge_max_tokens=10000),
    )
    raw_output = run_coro(
        SubmitSolution().acall(
            solution_type="direct_answer", task_report="done", handoff="{}"
        )
    )
    state = SimpleNamespace(
        messages=[
            {
                "type": "function_call_output",
                "call_id": "finish",
                "output": raw_output,
            }
        ],
        parent_state_id=None,
        current_user_intent=[],
        user_intent_log=[],
        finish_gate_rejection_count=0,
    )
    digest = FinishGateTraceDigest(
        trace_digest_text="saved trace", digest_chars=11
    )
    with (
        patch(
            "gc_core.summarize.llm.arequest_chat_completion_text",
            side_effect=[first, retry],
        ) as model,
        patch(
            "gc_core.intent_guard.guard.build_finish_gate_trace_digest",
            return_value=digest,
        ),
        patch(
            "gc_core.intent_guard.final_report_consistency.build_finish_gate_trace_digest",
            return_value=digest,
        ),
    ):
        decision = run_coro(
            FinishGateGuard(ctx=ctx, state=state).aevaluate_turn_finish(
                finish_handoff_arguments={"solution_type": "direct_answer"}
            )
        )
    assert model.call_count == calls
    assert run_coro(
        apply_finish_gate_to_turn_finish_output_in_state(
            state=state, finish_call_id="finish", decision=decision
        )
    )
    output = json.loads(state.messages[0]["output"])
    assert output["status"] == "rejected_by_finish_gate"
    assert output["finish_gate"]["trace_gaps"] == gaps
    assert output["finish_gate"]["report_mismatches"] == mismatches
