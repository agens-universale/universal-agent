import json
import os
import tempfile
import unittest

from gc_core.intent_log import deserialize_list
from gc_core.message_format.serialization import (
    calc_history_chars_for_trigger,
    calc_messages_chars_total,
    message_text_for_summary,
)
from gc_core.thread_store import (
    ThreadSession,
    append_new_thread_messages,
    load_thread_meta,
    save_thread_meta,
)
from gc_testkit.intent_fixtures import intent_user_ask_texts


# `unittest.TestCase` НЕ запускает корутинные тесты: метод просто возвращает
# корутину, и проверка молча не выполняется.
class ThreadSummaryTest(unittest.IsolatedAsyncioTestCase):
    def test_calc_messages_chars_total_uses_message_text_fields(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"type": "function_call", "name": "calc", "arguments": {"a": 1}},
            {"type": "function_call_output", "output": {"ok": True}},
        ]
        expected = sum(len(message_text_for_summary(m)) for m in messages)
        self.assertEqual(calc_messages_chars_total(messages), expected)

    def test_calc_history_chars_for_trigger_omits_role_wrappers(self) -> None:
        messages = [{"role": "user", "content": "hello"}]
        raw = calc_history_chars_for_trigger(messages)
        serialized = calc_messages_chars_total(messages)
        self.assertEqual(raw, 5)
        self.assertGreater(serialized, raw)

    def test_message_text_for_summary_tool_call_aliases_match_function_style(
        self,
    ) -> None:
        fc = {
            "type": "function_call",
            "name": "read",
            "arguments": "{}",
        }
        tc = {
            "type": "tool_call",
            "name": "read",
            "arguments": "{}",
            "call_id": "c1",
        }
        self.assertTrue(message_text_for_summary(tc).startswith("[tool_call]"))
        self.assertIn("read", message_text_for_summary(tc))
        self.assertIn("read", message_text_for_summary(fc))
        self.assertEqual(
            calc_history_chars_for_trigger([fc]),
            calc_history_chars_for_trigger([tc]),
        )

    async def test_archive_and_compact_summary_payload_creates_archive_and_new_trace(
        self,
    ) -> None:
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
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "notes_md": "old notes",
                    "previous_response_id": "resp_prev",
                    "backend_api_mode": "chat_completions",
                    "chat_completions_system_prompt_rendered": "SYSTEM",
                    "custom_meta_key": "preserve-me",
                },
            )

            session = await ThreadSession.afrom_config(
                config_dict={"threads_data_path": tmp_dir},
                system_id=system_id,
                thread_id=thread_id,
            )
            result = await session.aarchive_and_compact_summary_payload(
                {
                    "summary_message_content": "compressed summary",
                    "shared": {
                        "working_memory": {
                            "state": "## Thread summary\n\ncompressed summary"
                        },
                        "notes_md": "",
                    },
                    "user_intent_log": ["req1", "req2"],
                    "dialog_steps_offset": 12,
                }
            )

            suffix = result["archive_suffix"]
            archived_data = os.path.join(
                tmp_dir, f"data_cli_{thread_id}__pre_summary__{suffix}.jsonl"
            )
            archived_meta = os.path.join(
                tmp_dir, f"meta_cli_{thread_id}__pre_summary__{suffix}.json"
            )
            self.assertTrue(os.path.exists(archived_data))
            self.assertTrue(os.path.exists(archived_meta))

            data_path = os.path.join(tmp_dir, f"data_cli_{thread_id}.jsonl")
            with open(data_path, encoding="utf-8") as fd:
                rows = [json.loads(line) for line in fd if line.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["payload"]["role"], "user")
            self.assertIn("compressed summary", rows[0]["payload"]["content"])

            meta = load_thread_meta(tmp_dir, system_id, thread_id)
            self.assertEqual(
                meta.get("shared", {}).get("working_memory", {}).get("state"),
                "## Thread summary\n\ncompressed summary",
            )
            self.assertEqual(meta.get("backend_api_mode"), "chat_completions")
            self.assertEqual(meta.get("messages_count"), 1)
            self.assertEqual(meta.get("dialog_steps_offset"), 12)
            self.assertEqual(
                intent_user_ask_texts(
                    deserialize_list(meta.get("user_intent_log"))
                ),
                ["req1", "req2"],
            )
            self.assertIsNone(meta.get("previous_response_id"))
            self.assertEqual(
                meta.get("chat_completions_system_prompt_rendered"), "SYSTEM"
            )
            self.assertEqual(
                meta.get("created_at"), "2026-01-01T00:00:00+00:00"
            )
            self.assertEqual(meta.get("custom_meta_key"), "preserve-me")

    async def test_archive_and_compact_preserves_created_at_from_disk_when_missing_in_memory(
        self,
    ) -> None:
        """Regression: save_thread_meta may mint created_at on disk while self.meta lags."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            thread_id = "demo-thread-created-at"
            system_id = "cli"
            known: set = set()
            append_new_thread_messages(
                tmp_dir,
                system_id,
                thread_id,
                [{"role": "user", "content": "old message"}],
                known,
                start_seq=0,
            )
            original_created_at = "2026-01-01T00:00:00+00:00"
            save_thread_meta(
                tmp_dir,
                system_id,
                thread_id,
                {
                    "created_at": original_created_at,
                    "backend_api_mode": "chat_completions",
                },
            )

            session = await ThreadSession.afrom_config(
                config_dict={"threads_data_path": tmp_dir},
                system_id=system_id,
                thread_id=thread_id,
            )
            session.meta.pop("created_at", None)

            await session.aarchive_and_compact_summary_payload(
                {
                    "summary_message_content": "compressed summary",
                    "shared": {"working_memory": {}, "notes_md": ""},
                    "dialog_steps_offset": 1,
                }
            )

            meta = load_thread_meta(tmp_dir, system_id, thread_id)
            self.assertEqual(meta.get("created_at"), original_created_at)
            self.assertEqual(
                session.meta.get("created_at"), original_created_at
            )

    async def test_archive_and_compact_preserves_original_user_messages_in_dump(
        self,
    ) -> None:
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

            session = await ThreadSession.afrom_config(
                config_dict={"threads_data_path": tmp_dir},
                system_id=system_id,
                thread_id=thread_id,
            )
            summary_text = (
                "<dialog_summary>\n"
                "<original_user_messages>\n"
                "old request\n"
                "</original_user_messages>\n\n"
                "<summary>\n"
                "TASK_AND_CONTEXT:\n- keep this\n"
                "</summary>\n"
                "</dialog_summary>"
            )
            await session.aarchive_and_compact_summary_payload(
                {
                    "summary_message_content": summary_text,
                    "shared": {
                        "working_memory": {"state": "notes"},
                        "notes_md": "",
                    },
                }
            )

            data_path = os.path.join(tmp_dir, f"data_cli_{thread_id}.jsonl")
            with open(data_path, encoding="utf-8") as fd:
                rows = [json.loads(line) for line in fd if line.strip()]
            payload_content = rows[0]["payload"]["content"]
            self.assertIn("<original_user_messages>", payload_content)
            self.assertIn("<summary>", payload_content)


if __name__ == "__main__":
    unittest.main()
