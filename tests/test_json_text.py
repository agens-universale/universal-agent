import json
import unittest

from utils.json_text import format_json_for_display


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
