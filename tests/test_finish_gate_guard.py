import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from intent_log import ACTION_USER_ASK, append_user_ask, flush
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
                "strict_turn_finish_validation": True,
            }
        )
        state = SimpleNamespace(parent_state_id=None)
        guard = FinishGateGuard(ctx=ctx, state=state, logger=None)
        self.assertFalse(guard.enabled)
        d = guard.evaluate_turn_finish(finish_handoff_arguments={})
        self.assertEqual(d.outcome, "skipped")

    def test_iteration_limit_skips_llm(self) -> None:
        ctx = SimpleNamespace(
            config_dict={
                "backend_api_mode": "chat_completions",
                "strict_turn_finish_validation": True,
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
        d = guard.evaluate_turn_finish(finish_handoff_arguments={})
        self.assertEqual(d.outcome, "fail_open_limit")

    def test_reject_increments_counter(self) -> None:
        ctx = SimpleNamespace(
            config_dict={
                "backend_api_mode": "chat_completions",
                "strict_turn_finish_validation": True,
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
            d = guard.evaluate_turn_finish(finish_handoff_arguments={})
            self.assertEqual(d.outcome, "reject")
            self.assertEqual(state.finish_gate_rejection_count, 1)

    def test_fail_open_error_does_not_increment_rejection_count(self) -> None:
        ctx = SimpleNamespace(
            config_dict={
                "backend_api_mode": "chat_completions",
                "strict_turn_finish_validation": True,
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
            d = guard.evaluate_turn_finish(finish_handoff_arguments={})

        self.assertEqual(d.outcome, "fail_open_error")
        self.assertEqual(state.finish_gate_rejection_count, 0)

    def test_validate_receives_jsonl_intent_lines(self) -> None:
        ctx = SimpleNamespace(
            config_dict={
                "backend_api_mode": "chat_completions",
                "strict_turn_finish_validation": True,
                "finish_gate_max_iterations": 10,
            }
        )
        closed_turn = append_user_ask([], "closed ask", user_turn_seq=0)
        closed_turn, log, seq = flush(closed_turn, [], user_turn_seq=0)
        open_turn = append_user_ask(closed_turn, "open ask", user_turn_seq=seq)
        state = SimpleNamespace(
            parent_state_id=None,
            current_user_intent=open_turn,
            user_intent_log=log,
            finish_gate_rejection_count=0,
        )
        captured: dict[str, list[str]] = {}

        def _capture(**kwargs):
            captured["current"] = list(kwargs.get("current_user_requests") or [])
            captured["context"] = list(kwargs.get("context_user_messages") or [])
            return ValidationResult(
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

        with patch.object(
            intent_guard_guard_module,
            "validate_final_report_vs_user_intent",
            _capture,
        ):
            FinishGateGuard(ctx=ctx, state=state).evaluate_turn_finish(
                finish_handoff_arguments={}
            )

        assert len(captured["current"]) == 1
        row = json.loads(captured["current"][0])
        assert row["action"] == ACTION_USER_ASK
        assert row["content"] == "open ask"
        assert len(captured["context"]) == 1
        assert json.loads(captured["context"][0])["content"] == "closed ask"


if __name__ == "__main__":
    unittest.main()
