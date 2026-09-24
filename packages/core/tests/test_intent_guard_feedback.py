import unittest

from gc_core.intent_guard.feedback import (
    QUESTION_CONTINUE_HINT,
    TEXT_ANSWER_REJECT_TOOL_HINT,
    build_finish_gate_feedback,
    build_text_answer_reject_feedback,
)
from gc_core.intent_guard.validation_result import ValidationResult


def _v(**kwargs: object) -> ValidationResult:
    base = dict(
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
        is_question=False,
    )
    base.update(kwargs)
    return ValidationResult(**base)


class FinishGateFeedbackTest(unittest.TestCase):
    def test_accept_is_one_line(self) -> None:
        text = build_finish_gate_feedback(_v(ok=True))
        self.assertEqual(text, "Finish gate: accept.")

    def test_fix_report_no_redo_tools_wording(self) -> None:
        text = build_finish_gate_feedback(
            _v(
                ok=False,
                action="fix_report",
                trace_ok=True,
                report_ok=False,
                report_mismatches=["solution_type должен быть direct_answer"],
            )
        )
        self.assertTrue(text.startswith("Finish gate: fix_report."))
        self.assertIn("without repeating", text)
        self.assertIn("direct_answer", text)
        self.assertNotIn("ask_human", text.lower())

    def test_continue_work_lists_trace_gaps(self) -> None:
        text = build_finish_gate_feedback(
            _v(
                ok=False,
                action="continue_work",
                trace_ok=False,
                report_ok=True,
                trace_gaps=["нет fs_write_file для X"],
            )
        )
        self.assertIn("finish gate: continue_work.", text.lower())
        self.assertIn("fs_write_file", text)
        self.assertIn("Take the missing step", text)

    def test_continue_work_is_question_uses_question_hint(self) -> None:
        text = build_finish_gate_feedback(
            _v(
                ok=False,
                action="continue_work",
                trace_ok=False,
                report_ok=True,
                is_question=True,
                trace_gaps=["blocking clarification"],
            )
        )
        self.assertIn(QUESTION_CONTINUE_HINT, text)
        self.assertIn("blocking clarification", text)
        self.assertNotIn("Take the missing step", text)
        self.assertNotIn("ask_human", text.lower())

    def test_fix_report_and_continue_both_blocks(self) -> None:
        text = build_finish_gate_feedback(
            _v(
                ok=False,
                action="fix_report_and_continue",
                trace_ok=False,
                report_ok=False,
                trace_gaps=["gap"],
                report_mismatches=["mismatch"],
            )
        )
        self.assertIn("gap", text)
        self.assertIn("mismatch", text)
        self.assertIn("Do not repeat", text)
        self.assertIn("Close trace gaps first", text)

    def test_fix_report_and_continue_is_question_uses_question_hint(
        self,
    ) -> None:
        text = build_finish_gate_feedback(
            _v(
                ok=False,
                action="fix_report_and_continue",
                trace_ok=False,
                report_ok=False,
                is_question=True,
                trace_gaps=["gap"],
                report_mismatches=["mismatch"],
            )
        )
        self.assertIn(QUESTION_CONTINUE_HINT, text)
        self.assertIn("gap", text)
        self.assertIn("mismatch", text)
        self.assertNotIn("Close trace gaps first", text)
        self.assertNotIn("Take the missing step", text)
        self.assertNotIn("ask_human", text.lower())

    def test_fix_report_keeps_old_text_even_with_is_question(self) -> None:
        text = build_finish_gate_feedback(
            _v(
                ok=False,
                action="fix_report",
                trace_ok=True,
                report_ok=False,
                is_question=True,
                report_mismatches=["bad solution_type"],
            )
        )
        self.assertIn("without repeating", text)
        self.assertNotIn(QUESTION_CONTINUE_HINT, text)


class TextAnswerRejectFeedbackTest(unittest.TestCase):
    def test_is_question_omits_next_step_tool_hint(self) -> None:
        text = build_text_answer_reject_feedback(
            _v(
                ok=False,
                action="continue_work",
                trace_ok=False,
                report_ok=True,
                is_question=True,
                trace_gaps=["need user choice"],
            )
        )
        self.assertIn(QUESTION_CONTINUE_HINT, text)
        self.assertNotIn(TEXT_ANSWER_REJECT_TOOL_HINT, text)
        self.assertNotIn("next step", text.lower())

    def test_is_question_false_keeps_tool_hint(self) -> None:
        text = build_text_answer_reject_feedback(
            _v(
                ok=False,
                action="continue_work",
                trace_ok=False,
                report_ok=True,
                is_question=False,
                trace_gaps=["need to run tests"],
            )
        )
        self.assertIn(TEXT_ANSWER_REJECT_TOOL_HINT, text)
        self.assertIn("Take the missing step", text)


if __name__ == "__main__":
    unittest.main()
