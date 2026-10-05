"""Slice raw_summary перед dialog_summary (шаг 6 спеки)."""

import types
import unittest
from unittest.mock import patch

from gc_core.summarize.compaction import arun_summary_compaction
from gc_core.summarize.summary_spec import (
    agent_summary_slice_mode,
    hint_section_tags,
    slice_summary,
)
from gc_core.summarize.working_memory_gateway import WorkingMemoryDeltas
from gc_testkit import run_coro
from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests
from gc_testkit.settings import fake_ctx


class CompactionSummarySliceTest(unittest.TestCase):
    def test_agent_summary_slice_mode(self) -> None:
        self.assertEqual(
            agent_summary_slice_mode(num_batches=1, patch_executed=True),
            "short",
        )
        self.assertEqual(
            agent_summary_slice_mode(num_batches=1, patch_executed=False),
            "full",
        )
        self.assertEqual(
            agent_summary_slice_mode(num_batches=2, patch_executed=False),
            "full",
        )

    @patch("gc_core.summarize.compaction.log_compaction_completed")
    @patch("gc_core.summarize.compaction.log_summary_message_body")
    @patch("gc_core.summarize.compaction.log_summary_raw_text")
    @patch("gc_core.summarize.compaction.log_compaction_started")
    def test_single_batch_patch_short_slice_in_dialog_and_trace(
        self,
        _start,
        _raw_log,
        _body_log,
        _done,
    ) -> None:
        full_xml = valid_summary_xml_for_tests(seed="ctx")
        trace = {"memory_patch": {"executed": True}, "final_output": {}}
        seen: dict = {}

        async def fake_summarize(**_kwargs):
            return {
                "raw_summary": full_xml,
                "num_batches": 1,
                "last_dialog_steps": "",
                "memory_append_deltas": WorkingMemoryDeltas(),
                "memory_replace_deltas": WorkingMemoryDeltas(),
                "memory_append_touched": [],
                "memory_replace_touched": [],
                "trace": trace,
            }

        def fake_apply(**kwargs):
            seen["raw_summary"] = kwargs["raw_summary"]
            return "dialog-body"

        state = types.SimpleNamespace(
            thread_id="t1",
            messages=[{"role": "user", "content": "x" * 500}],
            user_intent_log=[],
            current_user_intent=[],
            dialog_steps_offset=0,
            previous_response_id="prev",
            shared=types.SimpleNamespace(
                working_memory={
                    "goal": "",
                    "constraints": "",
                    "state": "",
                    "knowledge": "",
                }
            ),
        )
        ctx = fake_ctx({"summary": {}}, logger=None)

        with (
            patch(
                "gc_core.summarize.compaction.asummarize_dialog_via_chat_completions",
                fake_summarize,
            ),
            patch(
                "gc_core.summarize.compaction.apply_summary_state_patch",
                fake_apply,
            ),
        ):
            run_coro(
                arun_summary_compaction(
                    ctx=ctx,
                    state=state,
                    trigger_reason="test",
                    is_cli_mode=False,
                )
            )

        short = slice_summary(full_xml, "short")
        self.assertEqual(seen["raw_summary"], short)
        for tag in hint_section_tags():
            self.assertNotIn(f"<{tag}>", seen["raw_summary"])
        self.assertEqual(trace["final_output"]["agent_summary_slice"], "short")
        self.assertEqual(trace["final_output"]["raw_summary"], full_xml)
        self.assertIn(short, trace["final_output"]["agent_summary_text"])

    @patch("gc_core.summarize.compaction.log_compaction_completed")
    @patch("gc_core.summarize.compaction.log_summary_message_body")
    @patch("gc_core.summarize.compaction.log_summary_raw_text")
    @patch("gc_core.summarize.compaction.log_compaction_started")
    def test_multi_batch_patch_skipped_full_slice_in_dialog_and_trace(
        self,
        _start,
        _raw_log,
        _body_log,
        _done,
    ) -> None:
        full_xml = valid_summary_xml_for_tests(seed="multi")
        trace = {
            "memory_patch": {
                "executed": False,
                "reason": "skipped_multi_batch",
            },
            "final_output": {},
        }
        seen: dict = {}

        async def fake_summarize(**_kwargs):
            return {
                "raw_summary": full_xml,
                "num_batches": 2,
                "last_dialog_steps": "",
                "memory_append_deltas": WorkingMemoryDeltas(),
                "memory_replace_deltas": WorkingMemoryDeltas(),
                "memory_append_touched": [],
                "memory_replace_touched": [],
                "trace": trace,
            }

        def fake_apply(**kwargs):
            seen["raw_summary"] = kwargs["raw_summary"]
            return "dialog-body"

        state = types.SimpleNamespace(
            thread_id="t1",
            messages=[{"role": "user", "content": "x" * 500}],
            user_intent_log=[],
            current_user_intent=[],
            dialog_steps_offset=0,
            previous_response_id="prev",
            shared=types.SimpleNamespace(
                working_memory={
                    "goal": "",
                    "constraints": "",
                    "state": "",
                    "knowledge": "",
                }
            ),
        )
        ctx = fake_ctx({"summary": {}}, logger=None)

        with (
            patch(
                "gc_core.summarize.compaction.asummarize_dialog_via_chat_completions",
                fake_summarize,
            ),
            patch(
                "gc_core.summarize.compaction.apply_summary_state_patch",
                fake_apply,
            ),
        ):
            run_coro(
                arun_summary_compaction(
                    ctx=ctx,
                    state=state,
                    trigger_reason="test",
                    is_cli_mode=False,
                )
            )

        full_slice = slice_summary(full_xml, "full")
        self.assertEqual(seen["raw_summary"], full_slice)
        for tag in hint_section_tags():
            self.assertIn(f"<{tag}>", seen["raw_summary"])
        self.assertEqual(trace["final_output"]["agent_summary_slice"], "full")
        self.assertEqual(trace["final_output"]["raw_summary"], full_xml)
        self.assertEqual(
            trace["final_output"]["agent_summary_text"], full_slice
        )
        self.assertFalse(trace["memory_patch"]["executed"])
