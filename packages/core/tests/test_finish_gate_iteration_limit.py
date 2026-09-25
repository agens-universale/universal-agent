"""Finish gate iteration limit — SPEC §10."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gc_core.intent_guard import guard as intent_guard_guard_module
from gc_core.intent_guard.guard import (
    FinishGateGuard,
    resolve_finish_gate_max_rejections,
)
from gc_core.intent_guard.validation_result import ValidationResult
from gc_testkit import run_coro


def _reject_validation(**_kwargs):
    return ValidationResult(
        ok=False,
        action="continue_work",
        trace_ok=False,
        report_ok=True,
        trace_gaps=["gap"],
        report_mismatches=[],
        missing_requirements=["gap"],
        conflicts=[],
        coverage_score=0.0,
        feedback_for_agent="raw llm text must not leak",
    )


class FinishGateIterationLimitTest(unittest.TestCase):
    def test_subagent_default_limit_is_three(self) -> None:
        sub_state = SimpleNamespace(parent_state_id="orch-1")
        self.assertEqual(resolve_finish_gate_max_rejections({}, sub_state), 3)

    def test_zero_config_falls_back_to_ten_not_unlimited(self) -> None:
        main_state = SimpleNamespace(parent_state_id=None)
        self.assertEqual(
            resolve_finish_gate_max_rejections(
                {"finish_gate_max_iterations": 0}, main_state
            ),
            10,
        )

    def test_third_submit_fail_open_after_two_rejects(self) -> None:
        ctx = SimpleNamespace(
            config_dict={
                "backend_api_mode": "chat_completions",
                "strict_turn_finish_validation": True,
                "finish_gate_max_iterations": 2,
            }
        )
        state = SimpleNamespace(
            parent_state_id=None,
            current_user_intent=["task"],
            user_intent_log=["task"],
            finish_gate_rejection_count=0,
        )
        validate_calls: list[int] = []

        async def _counting_reject(**kwargs):
            validate_calls.append(1)
            return _reject_validation(**kwargs)

        with patch.object(
            intent_guard_guard_module,
            "avalidate_final_report_vs_user_intent",
            _counting_reject,
        ):
            guard = FinishGateGuard(ctx=ctx, state=state)
            d1 = run_coro(
                guard.aevaluate_turn_finish(finish_handoff_arguments={})
            )
            d2 = run_coro(
                guard.aevaluate_turn_finish(finish_handoff_arguments={})
            )
            d3 = run_coro(
                guard.aevaluate_turn_finish(finish_handoff_arguments={})
            )

        self.assertEqual(d1.outcome, "reject")
        self.assertEqual(d2.outcome, "reject")
        self.assertEqual(state.finish_gate_rejection_count, 2)
        self.assertEqual(d3.outcome, "fail_open_limit")
        self.assertEqual(len(validate_calls), 2)


if __name__ == "__main__":
    unittest.main()
