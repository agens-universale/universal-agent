import unittest

from gc_core.message_format.dialog_history import (
    format_last_dialog_steps,
    group_into_turns,
    split_turns_into_char_batches,
)
from gc_core.message_policy.user_message_policy import append_user_log_entries
from gc_core.summarize.summary_validation import is_valid_summary_text, validate_summary_text
from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests


class ThreadSummarizerTest(unittest.TestCase):
    def test_append_user_log_entries_skips_technical_user_message(self) -> None:
        messages = [
            {
                "role": "user",
                "content": 'client has non-empty thread notes; call `thread_notes` with action="read" to load them.',
            },
            {"role": "user", "content": "There is no tool call in Agent response. Please send appropriate tool call"},
            {"role": "user", "content": "❌ Tool Argument Validation Failed. Expected schema: {...}"},
            {"role": "user", "content": "<dialog_summary>\n...\n</dialog_summary>"},
            {"role": "user", "content": "Сделай рефакторинг parser слоя"},
            {"role": "assistant", "content": "ok"},
        ]
        log = append_user_log_entries([], messages)
        self.assertEqual(log, ["Сделай рефакторинг parser слоя"])

    def test_is_valid_summary_text(self) -> None:
        self.assertTrue(is_valid_summary_text(valid_summary_xml_for_tests()))
        self.assertFalse(is_valid_summary_text(""))
        ok, reason = validate_summary_text("[assistant] raw transcript")
        self.assertFalse(is_valid_summary_text("[assistant] raw transcript"))
        self.assertEqual(reason, "missing_structure")
        self.assertFalse(is_valid_summary_text("TASK_AND_CONTEXT:\n- short prose"))
        preamble = "[assistant] preamble\n\n"
        self.assertTrue(is_valid_summary_text(preamble + valid_summary_xml_for_tests()))
        ok, reason = validate_summary_text("<summary><task_and_context>x</task_and_context></summary>")
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_structure")

    def test_format_last_dialog_steps_applies_per_message_limit(self) -> None:
        long_text = "x" * 6000
        steps = format_last_dialog_steps(
            [{"role": "user", "content": long_text}],
            max_steps=5,
            message_max_chars=5000,
        )
        self.assertIn('<cutted_envelope truncated="true"', steps)
        self.assertNotIn("TEXT CUTTED", steps)

    def test_format_last_dialog_steps_keeps_tool_call_and_output_in_one_turn(self) -> None:
        steps = format_last_dialog_steps(
            [
                {"type": "function_call", "call_id": "c1", "name": "fs_read_file", "arguments": {"path": "/tmp/a.txt"}},
                {"type": "function_call_output", "call_id": "c1", "output": {"ok": True}},
            ],
            max_steps=5,
            message_max_chars=5000,
        )
        self.assertEqual(steps.count("<step "), 1)
        self.assertIn("[function_call] fs_read_file", steps)
        self.assertIn("[function_call_output]", steps)

    def test_group_into_turns_supports_tool_call_aliases(self) -> None:
        turns = group_into_turns(
            [
                {"type": "tool_call", "call_id": "c1", "name": "fs_read_file", "arguments": "{}"},
                {"type": "tool_call_output", "call_id": "c1", "output": "ok"},
            ]
        )
        self.assertEqual(len(turns), 1)
        self.assertEqual(len(turns[0]), 2)

    def test_split_turns_into_char_batches(self) -> None:
        messages = [
            {"role": "user", "content": "a" * 2000},
            {"role": "assistant", "content": "b" * 2000},
            {"role": "user", "content": "c" * 2000},
        ]
        turns = group_into_turns(messages)
        batches = split_turns_into_char_batches(turns, batch_chars_limit=3500)
        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0]), 1)
        self.assertEqual(len(batches[1]), 1)

    def test_group_into_turns_keeps_function_call_and_output_together(self) -> None:
        messages = [
            {"role": "user", "content": "do x"},
            {"type": "function_call", "call_id": "c1", "name": "tool", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ]
        turns = group_into_turns(messages)
        self.assertEqual(len(turns), 2)
        self.assertEqual(len(turns[1]), 2)
        self.assertEqual(turns[1][0].get("type"), "function_call")
        self.assertEqual(turns[1][1].get("type"), "function_call_output")


if __name__ == "__main__":
    unittest.main()
