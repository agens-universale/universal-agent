import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gc_core.intent_guard.final_report_consistency import avalidate_final_report_vs_user_intent
from gc_core.intent_guard.trace_digest import FinishGateTraceDigest
from gc_testkit import run_coro


def _validator_json(**overrides: object) -> str:
    base = {
        "action": "accept",
        "trace_ok": True,
        "report_ok": True,
        "is_question": False,
        "trace_gaps": [],
        "report_mismatches": [],
    }
    base.update(overrides)
    return json.dumps(base)


class ValidateFinalReportConsistencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = SimpleNamespace(messages=[{"role": "user", "content": "спроси меня"}])
        self.ctx = SimpleNamespace(
            config_dict={},
            logger=None,
            auxiliary_llm=SimpleNamespace(
                max_tokens=8192,
                judge_max_tokens=10000,
                completion_kwargs={"reasoning_effort": "none"},
            ),
        )
        self.digest = FinishGateTraceDigest(
            trace_digest_text='<message n="1">\n[user] спроси меня\n</message>',
            digest_chars=40,
        )

    @patch("gc_core.summarize.llm.arequest_chat_completion_text")
    def test_payload_includes_trace_digest_and_no_user_intent_log(self, mock_llm) -> None:
        mock_llm.return_value = _validator_json()
        run_coro(
            avalidate_final_report_vs_user_intent(
                ctx=self.ctx,
                state=self.state,
                current_user_requests=["спроси меня"],
                context_user_messages=["старый"],
                finish_handoff_arguments={"solution_type": "full_solution"},
                trace_digest=self.digest,
            )
        )
        user_content = mock_llm.call_args.kwargs["messages"][1]["content"]
        self.assertIn("trace_digest_text", user_content)
        self.assertIn("<dialog_history>", user_content)
        self.assertNotIn("user_intent_log", user_content)

    @patch("gc_core.summarize.llm.arequest_chat_completion_text")
    def test_uses_two_axis_parser_fix_report(self, mock_llm) -> None:
        mock_llm.return_value = _validator_json(
            action="fix_report",
            trace_ok=True,
            report_ok=False,
            report_mismatches=["solution_type mismatch"],
        )
        result = run_coro(
            avalidate_final_report_vs_user_intent(
                ctx=self.ctx,
                state=self.state,
                current_user_requests=["спроси меня"],
                finish_handoff_arguments={"solution_type": "full_solution"},
                trace_digest=self.digest,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.action, "fix_report")
        self.assertTrue(result.trace_ok)
        self.assertFalse(result.report_ok)

    @patch("gc_core.summarize.llm.arequest_chat_completion_text")
    def test_broken_llm_json_retries_then_fails_open(self, mock_llm) -> None:
        mock_llm.return_value = "not json"
        result = run_coro(
            avalidate_final_report_vs_user_intent(
                ctx=self.ctx,
                state=self.state,
                current_user_requests=["x"],
                finish_handoff_arguments={},
                trace_digest=self.digest,
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "accept")
        self.assertEqual(mock_llm.call_count, 2)
        self.assertEqual(
            mock_llm.call_args_list[0].kwargs["request_label"],
            "intent_guard_finish_gate",
        )
        self.assertEqual(
            mock_llm.call_args_list[1].kwargs["request_label"],
            "intent_guard_finish_gate_retry",
        )
        retry_user = mock_llm.call_args_list[1].kwargs["messages"][1]["content"]
        self.assertIn("STRICT RETRY RULES", retry_user)
        self.assertIn("not json", retry_user)
        self.assertIn("Do NOT reason", retry_user)

    @patch("gc_core.summarize.llm.arequest_chat_completion_text")
    def test_unparseable_first_reply_retry_recovers(self, mock_llm) -> None:
        mock_llm.side_effect = [
            "<think>\nlong truncated reasoning without json\n",
            _validator_json(
                action="continue_work",
                trace_ok=False,
                report_ok=True,
                trace_gaps=["missing fs_write_file"],
            ),
        ]
        result = run_coro(
            avalidate_final_report_vs_user_intent(
                ctx=self.ctx,
                state=self.state,
                current_user_requests=["x"],
                finish_handoff_arguments={},
                trace_digest=self.digest,
            )
        )
        self.assertEqual(mock_llm.call_count, 2)
        self.assertFalse(result.ok)
        self.assertEqual(result.action, "continue_work")
        self.assertFalse(result.trace_ok)
        retry_user = mock_llm.call_args_list[1].kwargs["messages"][1]["content"]
        # Unclosed <think> is projected away before retry draft embed (спека 08).
        self.assertNotIn("long truncated reasoning without json", retry_user)
        draft_block = retry_user.split("Previous incomplete output:\n-----\n", 1)[1]
        draft_block = draft_block.split("\n-----\n", 1)[0]
        self.assertNotIn("<think>", draft_block)
        self.assertEqual(draft_block.strip(), "")
        self.assertEqual(
            mock_llm.call_args_list[0].kwargs["max_tokens"],
            10000,
        )
        self.assertEqual(
            mock_llm.call_args_list[1].kwargs["max_tokens"],
            10000,
        )

    @patch("gc_core.summarize.llm.arequest_chat_completion_text")
    def test_first_prompt_forbids_reasoning_out_loud(self, mock_llm) -> None:
        mock_llm.return_value = _validator_json()
        run_coro(
            avalidate_final_report_vs_user_intent(
                ctx=self.ctx,
                state=self.state,
                current_user_requests=["x"],
                finish_handoff_arguments={},
                trace_digest=self.digest,
            )
        )
        user_content = mock_llm.call_args.kwargs["messages"][1]["content"]
        self.assertIn("Do NOT reason out loud", user_content)
        self.assertIn("Return ONLY a single raw JSON object", user_content)

    @patch("gc_core.summarize.llm.arequest_chat_completion_text")
    def test_uses_auxiliary_llm_completion_kwargs(self, mock_llm) -> None:
        mock_llm.return_value = _validator_json()
        run_coro(
            avalidate_final_report_vs_user_intent(
                ctx=self.ctx,
                state=self.state,
                current_user_requests=["x"],
                finish_handoff_arguments={},
                trace_digest=self.digest,
            )
        )
        self.assertEqual(
            mock_llm.call_args.kwargs["payload_overrides"],
            {"reasoning_effort": "none"},
        )

    @patch("gc_core.summarize.llm.arequest_chat_completion_text")
    def test_request_label_finish_gate(self, mock_llm) -> None:
        mock_llm.return_value = _validator_json()
        run_coro(
            avalidate_final_report_vs_user_intent(
                ctx=self.ctx,
                state=self.state,
                current_user_requests=["x"],
                finish_handoff_arguments={},
                trace_digest=self.digest,
            )
        )
        self.assertEqual(mock_llm.call_count, 1)
        self.assertEqual(
            mock_llm.call_args.kwargs["request_label"],
            "intent_guard_finish_gate",
        )

    @patch("gc_core.summarize.llm.arequest_chat_completion_text")
    def test_payload_agent_kind_main_and_subagent(self, mock_llm) -> None:
        mock_llm.return_value = _validator_json()
        run_coro(
            avalidate_final_report_vs_user_intent(
                ctx=self.ctx,
                state=SimpleNamespace(messages=[], parent_state_id=None),
                current_user_requests=["x"],
                finish_handoff_arguments={},
                trace_digest=self.digest,
            )
        )
        main_content = mock_llm.call_args.kwargs["messages"][1]["content"]
        self.assertIn('"agent_kind": "main"', main_content)

        mock_llm.reset_mock()
        mock_llm.return_value = _validator_json()
        run_coro(
            avalidate_final_report_vs_user_intent(
                ctx=self.ctx,
                state=SimpleNamespace(messages=[], parent_state_id="parent-uuid"),
                current_user_requests=["x"],
                finish_handoff_arguments={},
                trace_digest=self.digest,
            )
        )
        sub_content = mock_llm.call_args.kwargs["messages"][1]["content"]
        self.assertIn('"agent_kind": "subagent"', sub_content)

    @patch("gc_core.intent_guard.final_report_consistency.build_finish_gate_trace_digest")
    @patch("gc_core.summarize.llm.arequest_chat_completion_text")
    def test_builds_digest_when_not_passed(self, mock_llm, mock_digest) -> None:
        mock_llm.return_value = _validator_json()
        mock_digest.return_value = self.digest
        run_coro(
            avalidate_final_report_vs_user_intent(
                ctx=self.ctx,
                state=self.state,
                current_user_requests=["x"],
                finish_handoff_arguments={},
            )
        )
        mock_digest.assert_called_once()


def test_guard_prompt_contains_turn_handoff_rule():
    """Промпт судьи содержит правило turn-handoff (доставка ассета + резолв
    bundled_root для editable и wheel)."""
    from gc_core.intent_guard.final_report_consistency import _load_guard_prompt

    prompt = _load_guard_prompt()
    assert "## Interactive turn-handoff (Axis A clarification)" in prompt
    assert "hands the turn back to the user" in prompt
    assert "set `trace_ok=true`" in prompt
    assert "near-repeat of an earlier one" in prompt


def test_guard_prompt_accepts_confirmation_before_action():
    """Судья знает: запрос одобрения перед действием — легитимный hand-off, не stalling."""
    from gc_core.intent_guard.final_report_consistency import _load_guard_prompt

    prompt = _load_guard_prompt()
    assert "explicit confirmation or approval **before** a consequential" in prompt


def test_guard_prompt_forbids_reasoning_in_reply():
    from gc_core.intent_guard.final_report_consistency import _load_guard_prompt

    prompt = _load_guard_prompt()
    assert "No reasoning in the reply" in prompt
    assert "Do **not** write chain-of-thought" in prompt


if __name__ == "__main__":
    unittest.main()
