import json
import unittest

from intent_guard.validation_result import (
    expected_action_for_axes,
    fail_open_validation_result,
    parse_validator_response,
)


def _raw(**overrides: object) -> str:
    base: dict[str, object] = {
        "action": "accept",
        "trace_ok": True,
        "report_ok": True,
        "is_question": False,
        "trace_gaps": [],
        "report_mismatches": [],
        "coverage_score": 1.0,
    }
    base.update(overrides)
    return json.dumps(base)


class ParseValidatorResponseTest(unittest.TestCase):
    def test_accept_valid_json(self) -> None:
        result = parse_validator_response(_raw())
        assert result is not None
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "accept")
        self.assertTrue(result.trace_ok)
        self.assertTrue(result.report_ok)
        self.assertFalse(result.is_question)

    def test_is_question_true_and_false(self) -> None:
        for flag in (True, False):
            result = parse_validator_response(_raw(is_question=flag))
            assert result is not None
            self.assertEqual(result.is_question, flag)

    def test_missing_is_question_returns_none(self) -> None:
        raw = json.dumps(
            {
                "action": "accept",
                "trace_ok": True,
                "report_ok": True,
            }
        )
        self.assertIsNone(parse_validator_response(raw))

    def test_non_bool_is_question_returns_none(self) -> None:
        self.assertIsNone(parse_validator_response(_raw(is_question="yes")))

    def test_fix_report_axis(self) -> None:
        result = parse_validator_response(
            _raw(
                action="fix_report",
                report_ok=False,
                report_mismatches=["solution_type mismatch"],
            )
        )
        assert result is not None
        self.assertFalse(result.ok)
        self.assertEqual(result.action, "fix_report")
        self.assertEqual(result.report_mismatches, ["solution_type mismatch"])

    def test_continue_work_axis(self) -> None:
        result = parse_validator_response(
            _raw(
                action="continue_work",
                trace_ok=False,
                trace_gaps=["no turn_ask_human"],
            )
        )
        assert result is not None
        self.assertFalse(result.ok)
        self.assertEqual(result.action, "continue_work")
        self.assertEqual(result.trace_gaps, ["no turn_ask_human"])

    def test_fix_report_and_continue_axis(self) -> None:
        result = parse_validator_response(
            _raw(
                action="fix_report_and_continue",
                trace_ok=False,
                report_ok=False,
                trace_gaps=["gap"],
                report_mismatches=["mismatch"],
            )
        )
        assert result is not None
        self.assertEqual(result.action, "fix_report_and_continue")

    def test_legacy_fields_duped_into_gaps_and_mismatches(self) -> None:
        result = parse_validator_response(
            _raw(
                action="continue_work",
                trace_ok=False,
                missing_requirements=["legacy gap"],
                conflicts=["legacy conflict"],
                trace_gaps=[],
                report_mismatches=[],
            )
        )
        assert result is not None
        self.assertEqual(result.trace_gaps, ["legacy gap"])
        self.assertEqual(result.missing_requirements, ["legacy gap"])
        self.assertEqual(result.report_mismatches, ["legacy conflict"])
        self.assertEqual(result.conflicts, ["legacy conflict"])

    def test_contradictory_ok_normalized_to_accept(self) -> None:
        with self.assertLogs("intent_guard.validation_result", level="WARNING") as logs:
            result = parse_validator_response(_raw(action="accept", ok=False))
        assert result is not None
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "accept")
        self.assertTrue(any("disagrees" in m for m in logs.output))

    def test_action_mismatch_normalized_with_warning(self) -> None:
        with self.assertLogs("intent_guard.validation_result", level="WARNING") as logs:
            result = parse_validator_response(
                _raw(action="continue_work", report_ok=False)
            )
        assert result is not None
        self.assertEqual(result.action, "fix_report")
        self.assertTrue(any("normalized" in m for m in logs.output))

    def test_invalid_json_returns_none(self) -> None:
        self.assertIsNone(parse_validator_response("not json at all"))

    def test_prose_preamble_before_json_is_parsed(self) -> None:
        """Finish-gate: prose before object (same idea as command judge brace slice)."""
        raw = "Кратко: всё ок.\n" + _raw()
        result = parse_validator_response(raw)
        assert result is not None
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "accept")

    def test_missing_axes_returns_none(self) -> None:
        self.assertIsNone(parse_validator_response(json.dumps({"action": "accept", "ok": True})))

    def test_invalid_action_returns_none(self) -> None:
        self.assertIsNone(parse_validator_response(_raw(action="retry")))

    def test_fail_open_helper(self) -> None:
        result = fail_open_validation_result()
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "accept")
        self.assertFalse(result.is_question)

    def test_expected_action_for_axes_table(self) -> None:
        self.assertEqual(
            expected_action_for_axes(trace_ok=True, report_ok=True),
            "accept",
        )
        self.assertEqual(
            expected_action_for_axes(trace_ok=True, report_ok=False),
            "fix_report",
        )
        self.assertEqual(
            expected_action_for_axes(trace_ok=False, report_ok=True),
            "continue_work",
        )
        self.assertEqual(
            expected_action_for_axes(trace_ok=False, report_ok=False),
            "fix_report_and_continue",
        )


if __name__ == "__main__":
    unittest.main()
