"""A failed summary must not replace the history sent to the main model."""

import json
import types

import pytest
from gc_core.aio import AsyncAgent
from gc_core.summarize.compaction import arun_summary_compaction
from gc_core.summarize.summary_spec import (
    assemble_summary_xml,
    render_output_shape_for_prompt,
)
from gc_core.thread_store import ThreadSession
from gc_testkit.develop_files import append_new_thread_messages
from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests
from gc_testkit.settings import fake_ctx, make_settings
from parity_harness import (
    ScriptedBackend,
    assistant_text,
    install,
    parity_config,
)


def _ctx(max_accumulated_summary_chars=20000, batch_chars=100000):
    return fake_ctx(
        {
            "agent.model": "test-model",
            "backend.completions_url": "http://example.test/v1/chat/completions",
            "logging.save_logs": False,
            "summary.incremental_batch_chars": batch_chars,
            "summary.max_accumulated_summary_chars": max_accumulated_summary_chars,
        },
        logger=None,
    )


@pytest.mark.parametrize("has_previous_summary", [False, True])
@pytest.mark.parametrize(
    "response_mode",
    [
        "malformed",
        "template",
        "compression",
        "oversize",
        "late_batch",
        "incomplete_compression",
        "truncated_compression",
    ],
)
async def test_failed_summary_preserves_live_and_saved_history(
    tmp_path, monkeypatch, has_previous_summary, response_mode
):
    compression_failure = response_mode in {
        "compression",
        "oversize",
        "incomplete_compression",
        "truncated_compression",
    }
    thread_id = f"failed-summary-{has_previous_summary}"
    prior = valid_summary_xml_for_tests(seed="confirmed-prior-context")
    messages = [{"role": "user", "content": "Original user requirement"}]
    if has_previous_summary:
        messages.insert(0, {"role": "user", "content": prior})
    if response_mode == "late_batch":
        messages.extend(
            {"role": "user", "content": f"Unsummarized source fact {i}"}
            for i in range(8)
        )
    append_new_thread_messages(
        str(tmp_path), "cli", thread_id, messages, set(), start_seq=0
    )
    session = await ThreadSession.afrom_config(
        settings=make_settings({"paths.threads_data_path": str(tmp_path)}),
        system_id="cli",
        thread_id=thread_id,
    )
    state = types.SimpleNamespace(
        default_model="test-model",
        thread_id=thread_id,
        shared=types.SimpleNamespace(working_memory={}),
        dialog_steps_offset=0,
        previous_response_id=None,
        user_intent_log=[],
        current_user_intent=[],
        user_turn_seq=0,
        messages=session.get_full_history_messages(),
    )
    before = list(state.messages)

    async def failed_completion(**kwargs):
        if response_mode == "truncated_compression":
            if kwargs["request_label"] == "summary_compress":
                return (
                    render_output_shape_for_prompt().split(
                        "</recent_artifacts>"
                    )[0]
                    + "</recent_artif"
                )
            return valid_summary_xml_for_tests(seed="valid-before-compression")
        if (
            response_mode == "late_batch"
            and kwargs["request_label"] == "summary_batch_1"
        ):
            return valid_summary_xml_for_tests(seed="first-successful-batch")
        if response_mode in {"oversize", "incomplete_compression"}:
            if kwargs["request_label"] == "summary_compress":
                return assemble_summary_xml(
                    {
                        "task_and_context": "x" * 101
                        if response_mode == "oversize"
                        else "short"
                    },
                    only_present=response_mode == "incomplete_compression",
                )
            return valid_summary_xml_for_tests(seed="valid-before-compression")
        if (
            response_mode == "template"
            or kwargs["request_label"] == "summary_compress"
        ):
            return render_output_shape_for_prompt()
        if response_mode == "compression":
            return valid_summary_xml_for_tests(seed="valid-before-compression")
        if kwargs["request_label"].startswith("summary_batch"):
            return "malformed initial answer"
        return "malformed repair answer"

    monkeypatch.setattr(
        "gc_core.summarize.thread_summarizer.arequest_summary_text",
        failed_completion,
    )
    monkeypatch.setattr(
        "gc_core.summarize.summary_llm_steps.arequest_summary_text",
        failed_completion,
    )

    async def prompt(_store):
        return "summary prompt"

    monkeypatch.setattr(
        "gc_core.summarize.prompt_loading.aload_summary_prompt",
        prompt,
    )

    async def empty_memory_patch(**_kwargs):
        return types.SimpleNamespace(request_messages=[], raw_response="")

    monkeypatch.setattr(
        "gc_core.summarize.memory_suggestions.arequest_patch_replace",
        empty_memory_patch,
    )
    archived = []

    async def archive(event):
        archived.append(event)
        await session.aarchive_and_compact_summary_payload(
            event["summary_payload"]
        )

    trace_path = tmp_path / "rejected-summary.jsonl"
    ctx = _ctx(
        100 if compression_failure else 20000,
        batch_chars=1 if response_mode == "late_batch" else 100000,
    )
    ctx.summary_trace_path = str(trace_path)
    with pytest.raises(ValueError, match="usable summary"):
        await arun_summary_compaction(
            ctx=ctx,
            state=state,
            trigger_reason="test",
            is_cli_mode=False,
            on_summary_applied=archive,
        )

    assert state.messages == before
    assert session.get_full_history_messages() == before
    reloaded = await ThreadSession.afrom_config(
        settings=make_settings({"paths.threads_data_path": str(tmp_path)}),
        system_id="cli",
        thread_id=thread_id,
    )
    assert reloaded.get_full_history_messages() == before
    assert not archived
    (record,) = [
        json.loads(line) for line in trace_path.read_text().splitlines()
    ]
    assert record["trigger_reason"] == "test"
    assert record["newly_summarized_message_id"] == ""
    trace = record["summary_trace"]
    assert trace["status"] == "rejected"
    batch = trace["step1_batches"][-1]
    if compression_failure:
        assert batch["compression"]["output_summary_text"]
    else:
        assert batch["repair"]["repaired_text"]
    if response_mode == "late_batch":
        assert len(trace["step1_batches"]) == 2
    if has_previous_summary:
        assert "confirmed-prior-context" in str(state.messages)


async def test_hidden_only_summary_does_not_replace_visible_context(
    monkeypatch,
):
    """The memory patch may hide active_constraints from the main model."""
    # Keep this case at the application boundary: a full XML document can be
    # structurally valid while its short agent slice has no useful content.
    from gc_core.summarize import compaction
    from gc_core.summarize.working_memory_gateway import WorkingMemoryDeltas

    hidden_only = assemble_summary_xml(
        {"active_constraints": "keep hidden fact"}
    )
    state = types.SimpleNamespace(
        default_model="test-model",
        thread_id="hidden-only",
        shared=types.SimpleNamespace(working_memory={}),
        dialog_steps_offset=0,
        previous_response_id=None,
        user_intent_log=[],
        current_user_intent=[],
        messages=[{"role": "user", "content": "Visible source fact"}],
    )
    before = list(state.messages)

    async def summarize(**_kwargs):
        return {
            "raw_summary": hidden_only,
            "num_batches": 1,
            "last_dialog_steps": "",
            "memory_append_deltas": WorkingMemoryDeltas(),
            "memory_replace_deltas": WorkingMemoryDeltas(),
            "trace": {"memory_patch": {"executed": True}, "final_output": {}},
        }

    monkeypatch.setattr(
        compaction, "asummarize_dialog_via_chat_completions", summarize
    )
    with pytest.raises(ValueError, match="usable summary"):
        await arun_summary_compaction(
            ctx=_ctx(), state=state, trigger_reason="test", is_cli_mode=False
        )
    assert state.messages == before


@pytest.mark.parametrize("summary_outcome", ["failed", "partial", "success"])
async def test_smoke_summary_preserves_context_in_next_main_request(
    tmp_path, monkeypatch, summary_outcome
):
    backend = install(
        monkeypatch,
        ScriptedBackend(
            [assistant_text("reply"), assistant_text("limit report")]
        ),
    )
    cfg = parity_config(
        tmp_path,
        monkeypatch,
        **{
            "agent.max_steps": 1,
            "summary.chat_completions_summary_trigger_chars": 1,
            "summary.incremental_batch_chars": 100000,
        },
    )
    summary_calls = []

    async def failed_completion(**kwargs):
        summary_calls.append(kwargs["request_label"])
        if summary_outcome == "success":
            return valid_summary_xml_for_tests(seed="Useful summary fact")
        if summary_outcome == "partial":
            bodies = (
                {"task_and_context": "Useful summary fact"}
                if kwargs["request_label"].startswith("summary_batch")
                else {"open_tasks": "- n/a"}
            )
            return assemble_summary_xml(bodies, only_present=True)
        if kwargs["request_label"].startswith("summary_batch"):
            return "malformed initial answer"
        return "malformed repair answer"

    async def prompt(_store):
        return "summary prompt"

    async def empty_memory_patch(**_kwargs):
        return types.SimpleNamespace(request_messages=[], raw_response="")

    monkeypatch.setattr(
        "gc_core.summarize.thread_summarizer.arequest_summary_text",
        failed_completion,
    )
    monkeypatch.setattr(
        "gc_core.summarize.summary_llm_steps.arequest_summary_text",
        failed_completion,
    )
    monkeypatch.setattr(
        "gc_core.summarize.prompt_loading.aload_summary_prompt", prompt
    )
    monkeypatch.setattr(
        "gc_core.summarize.memory_suggestions.arequest_patch_replace",
        empty_memory_patch,
    )

    async with await AsyncAgent.create(cfg) as agent:
        await agent.run("Unique user context for smoke", thread_id="d01-smoke")

    assert any(label.startswith("summary_batch") for label in summary_calls)
    assert ("summary_repair" in summary_calls) == (
        summary_outcome != "success"
    )
    assert backend.calls
    main_messages = backend.calls[0]["messages"]
    assert "Unique user context for smoke" in str(main_messages)
    if summary_outcome == "failed":
        assert "<summary>" not in str(main_messages)
    else:
        assert "Useful summary fact" in str(main_messages)
        assert "<summary>" in str(main_messages)
    backend.assert_clean()
