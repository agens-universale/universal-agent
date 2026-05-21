import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from intent_guard.final_report_consistency import validate_final_report_vs_user_intent
from intent_guard.trace_digest import FinishGateTraceDigest


def _validator_json(**overrides: object) -> str:
    base = {
        "action": "accept",
        "trace_ok": True,
        "report_ok": True,
        "trace_gaps": [],
        "report_mismatches": [],
    }
    base.update(overrides)
    return json.dumps(base)


class ValidateFinalReportConsistencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = SimpleNamespace(messages=[{"role": "user", "content": "спроси меня"}])
        self.ctx = SimpleNamespace(config_dict={}, logger=None)
        self.digest = FinishGateTraceDigest(
            trace_digest_text='<message n="1">\n[user] спроси меня\n</message>',
            digest_chars=40,
        )

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_payload_includes_trace_digest_and_no_user_intent_log(self, mock_llm) -> None:
        mock_llm.return_value = _validator_json()
        validate_final_report_vs_user_intent(
            ctx=self.ctx,
            state=self.state,
            current_user_requests=["спроси меня"],
            context_user_messages=["старый"],
            submit_solution_arguments={"solution_type": "full_solution"},
            trace_digest=self.digest,
        )
        user_content = mock_llm.call_args.kwargs["messages"][1]["content"]
        self.assertIn("trace_digest_text", user_content)
        self.assertIn("<dialog_history>", user_content)
        self.assertNotIn("user_intent_log", user_content)

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_uses_two_axis_parser_fix_report(self, mock_llm) -> None:
        mock_llm.return_value = _validator_json(
            action="fix_report",
            trace_ok=True,
            report_ok=False,
            report_mismatches=["solution_type mismatch"],
        )
        result = validate_final_report_vs_user_intent(
            ctx=self.ctx,
            state=self.state,
            current_user_requests=["спроси меня"],
            submit_solution_arguments={"solution_type": "full_solution"},
            trace_digest=self.digest,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.action, "fix_report")
        self.assertTrue(result.trace_ok)
        self.assertFalse(result.report_ok)

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_broken_llm_json_fails_open(self, mock_llm) -> None:
        mock_llm.return_value = "not json"
        result = validate_final_report_vs_user_intent(
            ctx=self.ctx,
            state=self.state,
            current_user_requests=["x"],
            submit_solution_arguments={},
            trace_digest=self.digest,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "accept")

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_request_label_finish_gate(self, mock_llm) -> None:
        mock_llm.return_value = _validator_json()
        validate_final_report_vs_user_intent(
            ctx=self.ctx,
            state=self.state,
            current_user_requests=["x"],
            submit_solution_arguments={},
            trace_digest=self.digest,
        )
        self.assertEqual(
            mock_llm.call_args.kwargs["request_label"],
            "intent_guard_finish_gate",
        )

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_payload_agent_kind_main_and_subagent(self, mock_llm) -> None:
        mock_llm.return_value = _validator_json()
        validate_final_report_vs_user_intent(
            ctx=self.ctx,
            state=SimpleNamespace(messages=[], parent_state_id=None),
            current_user_requests=["x"],
            submit_solution_arguments={},
            trace_digest=self.digest,
        )
        main_content = mock_llm.call_args.kwargs["messages"][1]["content"]
        self.assertIn('"agent_kind": "main"', main_content)

        mock_llm.reset_mock()
        mock_llm.return_value = _validator_json()
        validate_final_report_vs_user_intent(
            ctx=self.ctx,
            state=SimpleNamespace(messages=[], parent_state_id="parent-uuid"),
            current_user_requests=["x"],
            submit_solution_arguments={},
            trace_digest=self.digest,
        )
        sub_content = mock_llm.call_args.kwargs["messages"][1]["content"]
        self.assertIn('"agent_kind": "subagent"', sub_content)

    @patch("intent_guard.final_report_consistency.build_finish_gate_trace_digest")
    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_builds_digest_when_not_passed(self, mock_llm, mock_digest) -> None:
        mock_llm.return_value = _validator_json()
        mock_digest.return_value = self.digest
        validate_final_report_vs_user_intent(
            ctx=self.ctx,
            state=self.state,
            current_user_requests=["x"],
            submit_solution_arguments={},
        )
        mock_digest.assert_called_once()


if __name__ == "__main__":
    unittest.main()
