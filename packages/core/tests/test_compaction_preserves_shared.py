"""Компакция обязана сохранять ВЕСЬ shared-блок, а не два поля.

Дефект: ``run_summary_compaction`` собирал ``summary_payload["shared"]`` руками
из ``working_memory`` + ``notes_md``, а ``thread_store`` этим объектом ПОЛНОСТЬЮ
заменяет ``meta["shared"]``. Остальные поля (``confirm_rules``,
``fetch_url_cache``, …) молча исчезали с диска: детерминированно при ручном
``/compact`` (там нет post-run heal) и при обрыве рана.

Тест самодостаточен (без gc_testkit.helpers — тот модуль не собирается на Windows).
"""

import types
import unittest
from unittest.mock import patch

from gc_core.summarize.compaction import arun_summary_compaction
from gc_core.summarize.working_memory_gateway import WorkingMemoryDeltas
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx


def _state_with_full_shared():
    return types.SimpleNamespace(
        thread_id="t-shared",
        messages=[{"role": "user", "content": "x" * 500}],
        user_intent_log=[],
        current_user_intent=[],
        user_turn_seq=0,
        dialog_steps_offset=0,
        previous_response_id=None,
        shared=types.SimpleNamespace(
            working_memory={
                "goal": "цель",
                "constraints": "",
                "state": "",
                "knowledge": "",
            },
            notes_md="заметки",
            fetch_url_cache={"https://example.test": "cached body"},
            confirm_rules={"exec_command:git status": True},
        ),
    )


async def _fake_summarize(**_kwargs):
    return {
        "raw_summary": "<summary>ok</summary>",
        "num_batches": 1,
        "last_dialog_steps": "",
        "memory_append_deltas": WorkingMemoryDeltas(),
        "memory_replace_deltas": WorkingMemoryDeltas(),
        "memory_append_touched": [],
        "memory_replace_touched": [],
        "trace": {"memory_patch": {"executed": False}, "final_output": {}},
    }


class CompactionPreservesSharedTest(unittest.TestCase):
    @patch("gc_core.summarize.compaction.log_compaction_completed")
    @patch("gc_core.summarize.compaction.log_summary_message_body")
    @patch("gc_core.summarize.compaction.log_summary_raw_text")
    @patch("gc_core.summarize.compaction.log_compaction_started")
    def test_summary_payload_carries_whole_shared_block(self, *_logs) -> None:
        state = _state_with_full_shared()
        ctx = fake_ctx({"summary_params": {}}, logger=None)
        captured: dict = {}

        with (
            patch(
                "gc_core.summarize.compaction.asummarize_dialog_via_chat_completions",
                _fake_summarize,
            ),
            patch(
                "gc_core.summarize.compaction.apply_summary_state_patch",
                lambda **_kw: "dialog-body",
            ),
        ):
            run_coro(
                arun_summary_compaction(
                    ctx=ctx,
                    state=state,
                    trigger_reason="test",
                    is_cli_mode=False,
                    on_summary_applied=lambda event: captured.update(event),
                )
            )

        shared = captured["summary_payload"]["shared"]
        # Поля, которые молча терялись: они есть в SharedStateData и должны доехать.
        self.assertEqual(
            shared["confirm_rules"], {"exec_command:git status": True}
        )
        self.assertEqual(
            shared["fetch_url_cache"], {"https://example.test": "cached body"}
        )
        # И прежнее поведение не сломано.
        self.assertEqual(shared["working_memory"]["goal"], "цель")
        self.assertEqual(shared["notes_md"], "заметки")


if __name__ == "__main__":
    unittest.main()
