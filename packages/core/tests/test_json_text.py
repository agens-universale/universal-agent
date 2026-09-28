import json
import unittest

from gc_core.utils.json_text import (
    extract_json_candidate,
    format_json_for_display,
    parse_json_from_text,
)


class ExtractJsonCandidateTest(unittest.TestCase):
    def test_fenced_json(self) -> None:
        raw = 'noise\n```json\n{"a": 1}\n```\nmore'
        self.assertEqual(extract_json_candidate(raw), '{"a": 1}')

    def test_prose_before_object(self) -> None:
        raw = 'prefix\n{"a": 1, "b": 2}\n'
        self.assertEqual(extract_json_candidate(raw), '{"a": 1, "b": 2}')

    def test_starts_with_brace_unchanged(self) -> None:
        raw = '{"a": 1}'
        self.assertEqual(extract_json_candidate(raw), raw)

    def test_parse_json_from_text_with_preamble(self) -> None:
        raw = "ok\n" + json.dumps({"trace_ok": True, "report_ok": False})
        self.assertEqual(
            parse_json_from_text(raw),
            {"trace_ok": True, "report_ok": False},
        )


class FormatJsonForDisplayTest(unittest.TestCase):
    def test_none_returns_empty_string(self):
        self.assertEqual(format_json_for_display(None), "")

    def test_compact_json_string_is_pretty_printed(self):
        raw = json.dumps({"solution_type": "direct_answer", "reason": "done"})
        out = format_json_for_display(raw)
        self.assertIn('\n  "solution_type"', out)
        self.assertEqual(json.loads(out), json.loads(raw))

    def test_unwraps_embedded_json_string_fields(self):
        inner = {"result": "ok", "modes": {"raw": "plain text"}}
        raw = json.dumps(
            {
                "solution_type": "direct_answer",
                "handoff": json.dumps(inner, ensure_ascii=False),
            },
            ensure_ascii=False,
        )
        out = format_json_for_display(raw)
        parsed = json.loads(out)
        self.assertIsInstance(parsed["handoff"], dict)
        self.assertEqual(parsed["handoff"]["result"], "ok")

    def test_non_json_string_is_returned_as_is(self):
        self.assertEqual(format_json_for_display("plain text"), "plain text")

    def test_dict_input_is_pretty_printed(self):
        out = format_json_for_display({"a": 1, "b": [2, 3]})
        self.assertEqual(json.loads(out), {"a": 1, "b": [2, 3]})


if __name__ == "__main__":
    unittest.main()
