import types
from xml.etree import ElementTree

import pytest
from gc_core.summarize.summary_llm_steps import acompress_summary_text
from gc_core.summarize.summary_spec import assemble_summary_xml
from gc_core.summarize.summary_validation import SummaryRejectedError
from gc_core.summarize.thread_summarizer import (
    asummarize_dialog_via_chat_completions,
)
from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests
from gc_testkit.settings import fake_ctx


async def _run_summary_with_responses(monkeypatch, responses):
    calls = []

    async def response(**kwargs):
        label = kwargs["request_label"]
        calls.append(label)
        return responses[label]

    async def prompt(_store):
        return "prompt"

    async def memory_patch(**_kwargs):
        return types.SimpleNamespace(request_messages=[], raw_response="")

    monkeypatch.setattr(
        "gc_core.summarize.thread_summarizer.arequest_summary_text", response
    )
    monkeypatch.setattr(
        "gc_core.summarize.summary_llm_steps.arequest_summary_text", response
    )
    monkeypatch.setattr(
        "gc_core.summarize.prompt_loading.aload_summary_prompt", prompt
    )
    monkeypatch.setattr(
        "gc_core.summarize.memory_suggestions.arequest_patch_replace",
        memory_patch,
    )
    ctx = fake_ctx(
        {
            "agent.model": "test-model",
            "backend.completions_url": "http://example.test/v1/chat/completions",
            "summary.max_accumulated_summary_chars": 20000,
        },
        logger=None,
    )
    result = await asummarize_dialog_via_chat_completions(
        ctx=ctx,
        state=types.SimpleNamespace(default_model="test-model"),
        full_history_messages=[
            {"role": "user", "content": "Continue the saved task"},
            {"role": "assistant", "content": "Working on it"},
        ],
    )
    return result, calls


@pytest.mark.asyncio
async def test_primary_ignores_prose_tag_mention(monkeypatch):
    final = valid_summary_xml_for_tests(seed="confirmed context")
    result, calls = await _run_summary_with_responses(
        monkeypatch,
        {
            "summary_batch_1": "I will now output the <summary> block.\n"
            + final
        },
    )
    assert result["raw_summary"] == final
    assert calls == ["summary_batch_1"]


@pytest.mark.asyncio
async def test_repair_ignores_prose_tag_mention(monkeypatch):
    final = valid_summary_xml_for_tests(seed="repaired context")
    result, calls = await _run_summary_with_responses(
        monkeypatch,
        {
            "summary_batch_1": "Incomplete summary",
            "summary_repair": "Fixed <summary>:\n" + final,
        },
    )
    assert result["raw_summary"] == final
    assert calls == ["summary_batch_1", "summary_repair"]
    assert result["trace"]["step1_batches"][0]["repair"]["repaired_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_text",
    [
        "I will now output the <summary> block.\n{xml}",
        "{xml}\nThat is the </summary> output.",
        "{xml}",
    ],
)
async def test_compression_ignores_prose_tag_mentions(
    monkeypatch, response_text
):
    final = valid_summary_xml_for_tests(seed="compressed context")
    if response_text == "{xml}":
        response_text = final.replace(
            "</open_tasks>",
            "</open_tasks>\nNote: <summary> is the output shape.",
        )
        expected = response_text
    else:
        response_text = response_text.format(xml=final)
        expected = final

    async def response(**_kwargs):
        return response_text

    monkeypatch.setattr(
        "gc_core.summarize.summary_llm_steps.arequest_summary_text", response
    )
    actual = await acompress_summary_text(
        ctx=None,
        state=None,
        summary_text=assemble_summary_xml({"task_and_context": "x" * 11000}),
        max_chars=10000,
        max_tokens=1000,
    )
    assert actual == expected


@pytest.mark.asyncio
async def test_compression_accepts_final_xml_after_draft_and_think(
    monkeypatch,
):
    final = valid_summary_xml_for_tests(seed="compressed final")
    draft = assemble_summary_xml(
        {"task_and_context": "draft"}, only_present=True
    )

    async def response(**_kwargs):
        return f"Analysis\n{draft}\n</think>\n{final}"

    monkeypatch.setattr(
        "gc_core.summarize.summary_llm_steps.arequest_summary_text", response
    )
    actual = await acompress_summary_text(
        ctx=None,
        state=None,
        summary_text=assemble_summary_xml({"task_and_context": "x" * 11000}),
        max_chars=10000,
        max_tokens=1000,
    )
    assert actual == final


@pytest.mark.asyncio
async def test_compression_ignores_summary_tokens_inside_cdata(monkeypatch):
    final = valid_summary_xml_for_tests(seed="final")
    final = final.replace(
        "final", "final <![CDATA[</summary> and </think> are text]]>"
    )
    assert ElementTree.fromstring(final).tag == "summary"

    async def response(**_kwargs):
        return final

    monkeypatch.setattr(
        "gc_core.summarize.summary_llm_steps.arequest_summary_text", response
    )
    actual = await acompress_summary_text(
        ctx=None,
        state=None,
        summary_text=assemble_summary_xml({"task_and_context": "x" * 11000}),
        max_chars=10000,
        max_tokens=1000,
    )
    assert actual == final


@pytest.mark.asyncio
async def test_compression_keeps_plain_text_inside_section(monkeypatch):
    final = valid_summary_xml_for_tests(seed="plain")
    final = final.replace(
        "plain", "plain 3 < 4 & </think> then <summary> is a label"
    )

    async def response(**_kwargs):
        return final

    monkeypatch.setattr(
        "gc_core.summarize.summary_llm_steps.arequest_summary_text", response
    )
    actual = await acompress_summary_text(
        ctx=None,
        state=None,
        summary_text=assemble_summary_xml({"task_and_context": "x" * 11000}),
        max_chars=10000,
        max_tokens=1000,
    )
    assert actual == final


@pytest.mark.asyncio
async def test_compression_keeps_outer_summary_around_quoted_example(
    monkeypatch,
):
    draft = valid_summary_xml_for_tests(seed="outer task context")
    example = valid_summary_xml_for_tests(seed="quoted example")
    outer = draft.replace(
        "outer task context", "outer task context\n</think>\n" + example
    )

    async def response(**_kwargs):
        return outer

    monkeypatch.setattr(
        "gc_core.summarize.summary_llm_steps.arequest_summary_text", response
    )
    actual = await acompress_summary_text(
        ctx=None,
        state=None,
        summary_text=assemble_summary_xml({"task_and_context": "x" * 11000}),
        max_chars=10000,
        max_tokens=1000,
    )
    assert actual == outer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trailer",
    [
        "\nThe log displays `</think>` literally.",
        "\nThe log displays ``</think>`` literally.",
        "\nA log excerpt follows:\n```text\n</think>\n```",
    ],
)
async def test_compression_ignores_literal_reasoning_marker_in_postamble(
    monkeypatch, trailer
):
    final = valid_summary_xml_for_tests(seed="confirmed context")

    async def response(**_kwargs):
        return final + trailer

    monkeypatch.setattr(
        "gc_core.summarize.summary_llm_steps.arequest_summary_text", response
    )
    actual = await acompress_summary_text(
        ctx=None,
        state=None,
        summary_text=assemble_summary_xml({"task_and_context": "x" * 11000}),
        max_chars=10000,
        max_tokens=1000,
    )
    assert actual == final


@pytest.mark.asyncio
async def test_unclosed_reasoning_draft_precedes_final_summary(monkeypatch):
    final = valid_summary_xml_for_tests(seed="confirmed context")

    async def response(**_kwargs):
        return (
            "<summary><task_and_context>Unfinished reasoning draft\n"
            "</think>\n" + final
        )

    monkeypatch.setattr(
        "gc_core.summarize.summary_llm_steps.arequest_summary_text", response
    )
    actual = await acompress_summary_text(
        ctx=None,
        state=None,
        summary_text=assemble_summary_xml({"task_and_context": "x" * 11000}),
        max_chars=10000,
        max_tokens=1000,
    )
    assert actual == final


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_text",
    [
        "<summary><task_and_context>unfinished",
        valid_summary_xml_for_tests(seed="first")
        + "\n"
        + valid_summary_xml_for_tests(seed="second"),
        valid_summary_xml_for_tests(seed="previous draft")
        + "\n<summary><task_and_context>truncated final",
        valid_summary_xml_for_tests(seed="previous draft")
        + "\n</think>\n<summary><task_and_context>truncated final",
        valid_summary_xml_for_tests(seed="previous draft") + "\n</think>",
    ],
)
async def test_compression_rejects_incomplete_or_ambiguous_xml(
    monkeypatch, response_text
):
    async def response(**_kwargs):
        return response_text

    monkeypatch.setattr(
        "gc_core.summarize.summary_llm_steps.arequest_summary_text", response
    )
    with pytest.raises(SummaryRejectedError, match="usable summary"):
        await acompress_summary_text(
            ctx=None,
            state=None,
            summary_text=assemble_summary_xml(
                {"task_and_context": "x" * 11000}
            ),
            max_chars=10000,
            max_tokens=1000,
        )
