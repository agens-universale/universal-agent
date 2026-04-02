import unittest

from summarize.dialog_history import format_last_dialog_steps, group_into_turns, split_turns_into_char_batches
from summarize.summary_validation import (
    _section_marker_hit,
    is_valid_summary_text,
    validate_summary_text,
)
from user_message_policy import append_user_log_entries


class ThreadSummarizerTest(unittest.TestCase):
    def test_append_user_log_entries_skips_technical_user_message(self) -> None:
        messages = [
            {"role": "user", "content": "client has non-empty thread notes; call `thread_notes` with action=\"read\" to load them."},
            {"role": "user", "content": "There is no tool call in Agent response. Please send appropriate tool call"},
            {"role": "user", "content": "❌ Tool Argument Validation Failed. Expected schema: {...}"},
            {"role": "user", "content": "<dialog_summary>\n...\n</dialog_summary>"},
            {"role": "user", "content": "Сделай рефакторинг parser слоя"},
            {"role": "assistant", "content": "ok"},
        ]
        log = append_user_log_entries([], messages)
        self.assertEqual(log, ["Сделай рефакторинг parser слоя"])

    def test_is_valid_summary_text(self) -> None:
        good_summary = (
            "TASK_AND_CONTEXT:\n- context details.\n\n"
            "GOALS:\n- goal.\n\n"
            "PLAN_AND_PROGRESS:\n- [DONE] something.\n\n"
            "NEXT_ACTIONS:\n- action.\n\n"
            "SPEC_AND_CONSTRAINTS:\n- constraints.\n\n"
            "IMPORTANT_ARTIFACTS:\n- src/summarize/thread_summarizer.py\n"
        )
        self.assertTrue(is_valid_summary_text(good_summary))
        human_headers = (
            "# Task and context\n"
            "- User wants a fact-check of acp2.md and acp3.md output.\n\n"
            "## Goals\n"
            "- Verify claims; produce corrected doc.\n\n"
            "### Plan and progress\n"
            "- [DONE] Read official docs.\n\n"
            "### Next actions\n"
            "- Draft acp3.md.\n\n"
            "### Spec and constraints\n"
            "- Preserve full article structure.\n\n"
            "### Risks and open questions\n"
            "- Minor site wording may change.\n\n"
            "### Important artifacts\n"
            "- prompts/chat_completions_summary_prompt.md\n"
        )
        self.assertTrue(
            is_valid_summary_text(human_headers),
            " spaced/markdown headings should satisfy soft section keywords",
        )
        self.assertFalse(
            _section_marker_hit(
                "Something and nothing and everything and more.",
                "TASK_AND_CONTEXT",
            ),
            "conjunction 'and' must not count toward TASK_AND_CONTEXT",
        )
        self.assertFalse(is_valid_summary_text(""))
        self.assertFalse(is_valid_summary_text("[assistant] raw transcript"))
        ok, reason = validate_summary_text("TASK_AND_CONTEXT:\n- short")
        self.assertFalse(ok)
        self.assertEqual(reason, "too_short")

    def test_format_last_dialog_steps_applies_per_message_limit(self) -> None:
        long_text = "x" * 6000
        steps = format_last_dialog_steps(
            [{"role": "user", "content": long_text}],
            max_steps=5,
            message_max_chars=5000,
        )
        self.assertIn("... [TEXT CUTTED]", steps)

    def test_format_last_dialog_steps_keeps_tool_call_and_output_in_one_turn(self) -> None:
        steps = format_last_dialog_steps(
            [
                {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": {"path": "/tmp/a.txt"}},
                {"type": "function_call_output", "call_id": "c1", "output": {"ok": True}},
            ],
            max_steps=5,
            message_max_chars=5000,
        )
        self.assertEqual(steps.count("<turn "), 1)
        self.assertIn("[function_call] read_file", steps)
        self.assertIn("[function_call_output]", steps)

    def test_group_into_turns_supports_tool_call_aliases(self) -> None:
        turns = group_into_turns(
            [
                {"type": "tool_call", "call_id": "c1", "name": "read_file", "arguments": "{}"},
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
        batches = split_turns_into_char_batches(
            turns, batch_chars_limit=3500
        )
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
