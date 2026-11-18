"""Binding constraints survive compaction when the memory patch misses them."""

import types
from pathlib import Path

import pytest
from gc_core.summarize.compaction import arun_summary_compaction
from gc_core.summarize.summary_spec import assemble_summary_xml
from gc_testkit.paths import REPO
from gc_testkit.settings import fake_ctx

SUMMARY = assemble_summary_xml(
    {
        "task_and_context": "Continue extracting receipt data into the workbook.",
        "open_tasks": "mode: execution\n- [TODO] Verify all rows.",
        "do_not_redo": "n/a",
        "last_verified_facts": "The extraction script exists in the workspace.",
        "active_constraints": "Keep the results sheet and the exact column order.",
        "recent_artifacts": "The workbook is the output artifact.",
    }
)

# An anonymized memory-patch response with no register tags.
SAVED_PATCH_RESPONSE = """The three edits were applied, but there's a remaining bug: `extract_total` references `_bucket()` which doesn't exist yet — it will NameError on re-run. Let me inspect the current function and fix it properly.

<function_call>
<function_call>
{process: fs_read_file, args: {"path": "/workspace/extract.py", "offset": 100, "limit": 55, "add_line_numbers": true, "reason": "Inspect current extract_total to fix _bucket wiring}}
</function>
</tool_call>"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "patch_response",
        "repair_response",
        "expected_memory",
        "in_summary",
        "active_constraint",
    ),
    [
        (
            SAVED_PATCH_RESPONSE,
            SAVED_PATCH_RESPONSE,
            "",
            True,
            "Keep the results sheet and the exact column order.",
        ),
        (
            SAVED_PATCH_RESPONSE,
            "<state>Verify the workbook rows.</state>",
            "",
            True,
            "Keep the results sheet and the exact column order.",
        ),
        (
            "<constraints>Keep the results sheet and the exact column order.</constraints>",
            "",
            "Keep the results sheet and the exact column order.",
            False,
            "Keep the results sheet and the exact column order.",
        ),
        (
            "<constraints>Never edit files in output/tmp/</constraints>",
            "",
            "Never edit files in output/tmp/",
            True,
            "Never edit files in output/",
        ),
    ],
    ids=[
        "saved-empty-patch",
        "partial-state-patch",
        "healthy-constraints-patch",
        "narrower-memory-rule",
    ],
)
async def test_compaction_keeps_constraints_in_sent_message(
    monkeypatch,
    patch_response,
    repair_response,
    expected_memory,
    in_summary,
    active_constraint,
):
    from gc_core.summarize import (
        compaction,
        memory_suggestions,
        thread_summarizer,
    )

    assert Path(compaction.__file__).resolve().is_relative_to(REPO.resolve())

    state = types.SimpleNamespace(
        default_model="test-model",
        thread_id="constraints-regression",
        shared=types.SimpleNamespace(working_memory={}),
        dialog_steps_offset=0,
        previous_response_id=None,
        user_intent_log=[],
        current_user_intent=[],
        messages=[{"role": "user", "content": "Extract the receipt data."}],
    )
    ctx = fake_ctx(
        {
            "agent.model": "test-model",
            "backend.completions_url": "http://example.test/v1/chat/completions",
            "summary.incremental_batch_chars": 100000,
        },
        logger=None,
    )

    async def summary_response(**_kwargs):
        return SUMMARY.replace(
            "Keep the results sheet and the exact column order.",
            active_constraint,
        )

    async def patch_response_from_llm(**_kwargs):
        return patch_response

    async def repair_response_from_llm(**_kwargs):
        return repair_response

    monkeypatch.setattr(
        thread_summarizer, "arequest_summary_text", summary_response
    )
    monkeypatch.setattr(
        memory_suggestions, "arequest_summary_text", patch_response_from_llm
    )
    monkeypatch.setattr(
        memory_suggestions,
        "arequest_memory_deltas_format_repair",
        repair_response_from_llm,
    )

    await arun_summary_compaction(
        ctx=ctx, state=state, trigger_reason="test", is_cli_mode=False
    )

    sent = state.messages[-1]["content"]
    assert '<technical type="dialog_summary">' in sent
    assert (
        f"<active_constraints>{active_constraint}</active_constraints>" in sent
    ) is in_summary
    assert state.shared.working_memory["constraints"] == expected_memory
