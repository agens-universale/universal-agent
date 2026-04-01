import json
import os
import tempfile
import unittest

from thread_store import (
    ThreadSession,
    append_new_thread_messages,
    calc_messages_chars_total,
    load_thread_meta,
    message_text_for_summary,
    save_thread_meta,
)


class ThreadSummaryTest(unittest.TestCase):
    def test_calc_messages_chars_total_uses_message_text_fields(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"type": "function_call", "name": "calc", "arguments": {"a": 1}},
            {"type": "function_call_output", "output": {"ok": True}},
        ]
        expected = sum(len(message_text_for_summary(m)) for m in messages)
        self.assertEqual(calc_messages_chars_total(messages), expected)

    def test_archive_and_compact_summary_payload_creates_archive_and_new_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            thread_id = "demo-thread"
            system_id = "cli"
            known = set()
            append_new_thread_messages(
                tmp_dir,
                system_id,
                thread_id,
                [{"role": "user", "content": "old message"}],
                known,
                start_seq=0,
            )
            save_thread_meta(
                tmp_dir,
                system_id,
                thread_id,
                {
                    "notes_md": "old notes",
                    "backend_api_mode": "chat_completions",
                    "chat_completions_system_prompt_rendered": "SYSTEM",
                },
            )

            session = ThreadSession.from_config(
                config_dict={"threads_data_path": tmp_dir},
                system_id=system_id,
                thread_id=thread_id,
            )
            result = session.archive_and_compact_summary_payload(
                {
                    "summary_message_content": "compressed summary",
                    "notes_md": "## Thread summary\n\ncompressed summary",
                    "user_messages_log": ["[user] req1", "[user] req2"],
                    "dialog_steps_offset": 12,
                }
            )

            suffix = result["archive_suffix"]
            archived_data = os.path.join(
                tmp_dir, f"data_cli_{thread_id}__summarized_{suffix}.jsonl"
            )
            archived_meta = os.path.join(
                tmp_dir, f"meta_cli_{thread_id}__summarized_{suffix}.json"
            )
            self.assertTrue(os.path.exists(archived_data))
            self.assertTrue(os.path.exists(archived_meta))

            data_path = os.path.join(tmp_dir, f"data_cli_{thread_id}.jsonl")
            with open(data_path, "r", encoding="utf-8") as fd:
                rows = [json.loads(line) for line in fd if line.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["payload"]["role"], "user")
            self.assertIn("compressed summary", rows[0]["payload"]["content"])

            meta = load_thread_meta(tmp_dir, system_id, thread_id)
            self.assertEqual(meta.get("notes_md"), "## Thread summary\n\ncompressed summary")
            self.assertEqual(meta.get("backend_api_mode"), "chat_completions")
            self.assertEqual(meta.get("messages_count"), 1)
            self.assertEqual(meta.get("dialog_steps_offset"), 12)
            self.assertEqual(meta.get("user_messages_log"), ["[user] req1", "[user] req2"])

    def test_archive_and_compact_strips_original_user_messages_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            thread_id = "demo-thread"
            system_id = "cli"
            known = set()
            append_new_thread_messages(
                tmp_dir,
                system_id,
                thread_id,
                [{"role": "user", "content": "old message"}],
                known,
                start_seq=0,
            )

            session = ThreadSession.from_config(
                config_dict={"threads_data_path": tmp_dir},
                system_id=system_id,
                thread_id=thread_id,
            )
            summary_text = (
                "<dialog_summary>\n"
                "<original_user_messages>\n"
                "[user] old request\n"
                "</original_user_messages>\n\n"
                "<summary>\n"
                "TASK_AND_CONTEXT:\n- keep this\n"
                "</summary>\n"
                "</dialog_summary>"
            )
            session.archive_and_compact_summary_payload(
                {
                    "summary_message_content": summary_text,
                    "notes_md": "notes",
                }
            )

            data_path = os.path.join(tmp_dir, f"data_cli_{thread_id}.jsonl")
            with open(data_path, "r", encoding="utf-8") as fd:
                rows = [json.loads(line) for line in fd if line.strip()]
            payload_content = rows[0]["payload"]["content"]
            self.assertNotIn("<original_user_messages>", payload_content)
            self.assertIn("<summary>", payload_content)


if __name__ == "__main__":
    unittest.main()
