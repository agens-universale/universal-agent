import unittest

from intent_guard.feedback import (
    build_finish_gate_feedback,
    build_finish_gate_reject_technical_user_content,
)
from intent_guard.validation_result import ValidationResult


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
                report_mismatches=["solution_type должен быть answer_question"],
            )
        )
        self.assertTrue(text.startswith("Finish gate: fix_report."))
        self.assertIn("без повторения", text)
        self.assertIn("answer_question", text)
        self.assertNotIn("ask_human", text.lower())

    def test_continue_work_lists_trace_gaps(self) -> None:
        text = build_finish_gate_feedback(
            _v(
                ok=False,
                action="continue_work",
                trace_ok=False,
                report_ok=True,
                trace_gaps=["нет write_to_file для X"],
            )
        )
        self.assertIn("finish gate: continue_work.", text.lower())
        self.assertIn("write_to_file", text)

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
        self.assertIn("Не повторяй", text)

    def test_reject_technical_user_content_includes_continue_work(self) -> None:
        feedback = build_finish_gate_feedback(
            _v(
                ok=False,
                action="fix_report",
                trace_ok=True,
                report_ok=False,
                report_mismatches=["solution_type должен быть answer_question"],
            )
        )
        text = build_finish_gate_reject_technical_user_content(feedback)
        self.assertIn("не соответствует интенту", text.lower())
        self.assertIn("продолжай работу", text.lower())
        self.assertIn("fix_report", text)
        self.assertIn("answer_question", text)


if __name__ == "__main__":
    unittest.main()
