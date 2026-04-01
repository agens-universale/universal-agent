import unittest

from thread_summarizer import (
    _format_last_dialog_steps,
    _group_into_turns,
    _split_turns_into_char_batches,
    _validate_summary_text,
    collect_user_messages_from_history,
    _is_valid_summary_text,
)


class ThreadSummarizerTest(unittest.TestCase):
    def test_collect_user_messages_from_history_skips_technical_message(self) -> None:
        messages = [
            {"role": "user", "content": "client has non-empty thread notes; call `thread_notes` with action=\"read\" to load them."},
            {"role": "user", "content": "There is no tool call in Agent response. Please send appropriate tool call"},
            {"role": "user", "content": "❌ Tool Argument Validation Failed. Expected schema: {...}"},
            {"role": "user", "content": "<dialog_summary>\n...\n</dialog_summary>"},
            {"role": "user", "content": "Сделай рефакторинг parser слоя"},
            {"role": "assistant", "content": "ok"},
        ]
        log = collect_user_messages_from_history(messages)
        self.assertEqual(len(log), 1)
        self.assertIn("Сделай рефакторинг", log[0])

    def test_is_valid_summary_text(self) -> None:
        good_summary = (
            "TASK_AND_CONTEXT:\n- context details.\n\n"
            "GOALS:\n- goal.\n\n"
            "PLAN_AND_PROGRESS:\n- [DONE] something.\n\n"
            "NEXT_ACTIONS:\n- action.\n\n"
            "SPEC_AND_CONSTRAINTS:\n- constraints.\n\n"
            "IMPORTANT_ARTIFACTS:\n- src/thread_summarizer.py\n"
        )
        self.assertTrue(_is_valid_summary_text(good_summary))
        self.assertFalse(_is_valid_summary_text(""))
        self.assertFalse(_is_valid_summary_text("[assistant] raw transcript"))
        ok, reason = _validate_summary_text("TASK_AND_CONTEXT:\n- short")
        self.assertFalse(ok)
        self.assertEqual(reason, "too_short")

    def test_format_last_dialog_steps_applies_per_message_limit(self) -> None:
        long_text = "x" * 6000
        steps = _format_last_dialog_steps(
            [{"role": "user", "content": long_text}],
            max_steps=5,
            message_max_chars=5000,
        )
        self.assertIn("... [TEXT CUTTED]", steps)

    def test_format_last_dialog_steps_keeps_tool_call_and_output_in_one_turn(self) -> None:
        steps = _format_last_dialog_steps(
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
        turns = _group_into_turns(
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
        turns = _group_into_turns(messages)
        batches = _split_turns_into_char_batches(
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
        turns = _group_into_turns(messages)
        self.assertEqual(len(turns), 2)
        self.assertEqual(len(turns[1]), 2)
        self.assertEqual(turns[1][0].get("type"), "function_call")
        self.assertEqual(turns[1][1].get("type"), "function_call_output")


if __name__ == "__main__":
    unittest.main()
