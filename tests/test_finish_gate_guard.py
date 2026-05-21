import unittest
from types import SimpleNamespace
from unittest.mock import patch

from intent_guard import guard as intent_guard_guard_module
from intent_guard.guard import FinishGateGuard, resolve_finish_gate_max_rejections
from intent_guard.validation_result import ValidationResult


class ResolveFinishGateMaxRejectionsTest(unittest.TestCase):
    def test_main_vs_subagent_key(self) -> None:
        main_state = SimpleNamespace(parent_state_id=None)
        sub_state = SimpleNamespace(parent_state_id="p1")
        cfg = {"finish_gate_max_iterations": 7, "subagent_finish_gate_max_iterations": 2}
        self.assertEqual(resolve_finish_gate_max_rejections(cfg, main_state), 7)
        self.assertEqual(resolve_finish_gate_max_rejections(cfg, sub_state), 2)

    def test_invalid_falls_back(self) -> None:
        state = SimpleNamespace(parent_state_id=None)
        self.assertEqual(
            resolve_finish_gate_max_rejections({"finish_gate_max_iterations": 0}, state),
            10,
        )


class FinishGateGuardTest(unittest.TestCase):
    def test_disabled_returns_skipped(self) -> None:
        ctx = SimpleNamespace(
            config_dict={
                "backend_api_mode": "responses",
                "strict_submit_solution_validation": True,
            }
        )
        state = SimpleNamespace(parent_state_id=None)
        guard = FinishGateGuard(ctx=ctx, state=state, logger=None)
        self.assertFalse(guard.enabled)
        d = guard.evaluate_submit_solution(submit_solution_arguments={})
        self.assertEqual(d.outcome, "skipped")

    def test_iteration_limit_skips_llm(self) -> None:
        ctx = SimpleNamespace(
            config_dict={
                "backend_api_mode": "chat_completions",
                "strict_submit_solution_validation": True,
                "finish_gate_max_iterations": 2,
            }
        )
        state = SimpleNamespace(
            parent_state_id=None,
            current_user_intent=["x"],
            user_intent_log=["x"],
            finish_gate_rejection_count=2,
        )
        guard = FinishGateGuard(ctx=ctx, state=state)
        self.assertTrue(guard.enabled)
        d = guard.evaluate_submit_solution(submit_solution_arguments={})
        self.assertEqual(d.outcome, "fail_open_limit")

    def test_reject_increments_counter(self) -> None:
        ctx = SimpleNamespace(
            config_dict={
                "backend_api_mode": "chat_completions",
                "strict_submit_solution_validation": True,
                "finish_gate_max_iterations": 10,
            }
        )
        state = SimpleNamespace(
            parent_state_id=None,
            current_user_intent=["x"],
            user_intent_log=["x"],
            finish_gate_rejection_count=0,
        )

        def _reject(**_kwargs):
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
                feedback_for_agent="",
            )

        with patch.object(
            intent_guard_guard_module,
            "validate_final_report_vs_user_intent",
            _reject,
        ):
            guard = FinishGateGuard(ctx=ctx, state=state)
            d = guard.evaluate_submit_solution(submit_solution_arguments={})
            self.assertEqual(d.outcome, "reject")
            self.assertEqual(state.finish_gate_rejection_count, 1)

    def test_fail_open_error_does_not_increment_rejection_count(self) -> None:
        ctx = SimpleNamespace(
            config_dict={
                "backend_api_mode": "chat_completions",
                "strict_submit_solution_validation": True,
                "finish_gate_max_iterations": 10,
            }
        )
        state = SimpleNamespace(
            parent_state_id=None,
            current_user_intent=["x"],
            user_intent_log=["x"],
            finish_gate_rejection_count=0,
        )

        def _raise(**_kwargs):
            raise RuntimeError("llm down")

        with patch.object(
            intent_guard_guard_module,
            "validate_final_report_vs_user_intent",
            _raise,
        ):
            guard = FinishGateGuard(ctx=ctx, state=state)
            d = guard.evaluate_submit_solution(submit_solution_arguments={})

        self.assertEqual(d.outcome, "fail_open_error")
        self.assertEqual(state.finish_gate_rejection_count, 0)


if __name__ == "__main__":
    unittest.main()
