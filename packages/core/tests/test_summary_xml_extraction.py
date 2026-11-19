import types
from xml.etree import ElementTree

import pytest
from gc_core.summarize.summary_llm_steps import acompress_summary_text
from gc_core.summarize.summary_spec import assemble_summary_xml, section_tags
from gc_core.summarize.summary_validation import SummaryRejectedError
from gc_core.summarize.summary_xml_extraction import extract_final_summary_xml
from gc_core.summarize.thread_summarizer import (
    asummarize_dialog_via_chat_completions,
)
from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests
from gc_testkit.settings import fake_ctx


async def _run_summary_with_responses(
    monkeypatch, responses, *, max_chars=20000
):
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
            "summary.max_accumulated_summary_chars": max_chars,
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
async def test_quoted_tag_mentions_do_not_hide_final_summary(monkeypatch):
    final = valid_summary_xml_for_tests(seed="verified context")
    response_text = (
        "Use `<summary>` to wrap the result.\nPrivate reasoning\n</think>\n"
        + final
        + "\nThe closing tag is `</summary>`."
    )
    result, calls = await _run_summary_with_responses(
        monkeypatch, {"summary_batch_1": response_text}
    )
    assert result["raw_summary"] == final
    assert result["trace"]["memory_patch"]["input"]["summary_text"] == final
    assert calls == ["summary_batch_1"]


@pytest.mark.parametrize("include_final", [False, True])
def test_double_backtick_xml_example_with_single_backticks(include_final):
    example = valid_summary_xml_for_tests(
        seed="Run `make test` as an example."
    )
    final = valid_summary_xml_for_tests(seed="verified work")
    response = "</think>\nExample of XML: ``" + example + "``"
    if include_final:
        assert extract_final_summary_xml(response + "\n" + final) == final
    else:
        with pytest.raises(SummaryRejectedError):
            extract_final_summary_xml(response)


@pytest.mark.asyncio
async def test_partial_final_summary_merges_with_partial_repair(monkeypatch):
    remaining = dict.fromkeys(section_tags()[1:], "repaired section")
    result, calls = await _run_summary_with_responses(
        monkeypatch,
        {
            "summary_batch_1": (
                "Reasoning\n</think>\n<summary>"
                "<task_and_context>confirmed partial</task_and_context>"
            ),
            "summary_repair": assemble_summary_xml(
                remaining, only_present=True
            ),
        },
    )
    assert "confirmed partial" in result["raw_summary"]
    assert "repaired section" in result["raw_summary"]
    assert (
        result["trace"]["step1_batches"][0]["repair"]["resolution"]
        == "merged_valid"
    )
    assert calls == ["summary_batch_1", "summary_repair"]


@pytest.mark.asyncio
async def test_truncated_primary_last_section_is_completed_by_repair(
    monkeypatch,
):
    primary = assemble_summary_xml(
        {
            "task_and_context": "confirmed context",
            "recent_artifacts": "Verification is recorded at reports/final-",
        }
    ).split("</recent_artifacts>")[0]
    result, calls = await _run_summary_with_responses(
        monkeypatch,
        {
            "summary_batch_1": "Reasoning\n</think>\n" + primary,
            "summary_repair": assemble_summary_xml(
                {"recent_artifacts": "Verify reports/final-check.json."},
                only_present=True,
            ),
        },
    )

    assert calls == ["summary_batch_1", "summary_repair"]
    assert (
        "<task_and_context>confirmed context</task_and_context>"
        in result["raw_summary"]
    )
    assert (
        "<recent_artifacts>Verify reports/final-check.json.</recent_artifacts>"
        in result["raw_summary"]
    )
    repair = result["trace"]["step1_batches"][0]["repair"]
    assert repair["missing_tags"] == ["recent_artifacts"]
    assert repair["resolution"] == "merged_valid"


@pytest.mark.asyncio
async def test_partial_repair_after_boundary_is_traced_and_merged(monkeypatch):
    result, calls = await _run_summary_with_responses(
        monkeypatch,
        {
            "summary_batch_1": "malformed first summary",
            "summary_repair": (
                "Repair notes\n</think>\n<summary>"
                "<task_and_context>confirmed repair</task_and_context>"
            ),
        },
    )
    assert "confirmed repair" in result["raw_summary"]
    repair = result["trace"]["step1_batches"][0]["repair"]
    assert repair["repaired_text"].startswith("<summary>")
    assert repair["resolution"] == "merged_valid"
    assert calls == ["summary_batch_1", "summary_repair"]


@pytest.mark.asyncio
async def test_ambiguous_repair_records_response_before_rejection(monkeypatch):
    first = valid_summary_xml_for_tests(seed="first draft")
    second = valid_summary_xml_for_tests(seed="second draft")
    raw_repair = first + "\n" + second
    with pytest.raises(SummaryRejectedError) as caught:
        await _run_summary_with_responses(
            monkeypatch,
            {
                "summary_batch_1": "malformed first summary",
                "summary_repair": raw_repair,
            },
        )
    repair = caught.value.trace["step1_batches"][0]["repair"]
    assert repair["repaired_text"] == raw_repair
    assert repair["resolution"] == "rejected"
    assert "ambiguous" in repair["extraction_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fenced_example", [False, True])
async def test_compression_uses_real_xml_after_quoted_example(
    monkeypatch, fenced_example
):
    draft = valid_summary_xml_for_tests(seed="draft context")
    example = valid_summary_xml_for_tests(seed="example context")
    final = valid_summary_xml_for_tests(seed="real context")
    if fenced_example:
        quoted = "\n```xml\n" + example + "\n```"
    else:
        quoted = " `" + example + "`"
    response_text = (
        "</think>\nExample of the XML format:"
        + quoted
        + "\nFinal summary follows:\n"
        + final
    )
    result, calls = await _run_summary_with_responses(
        monkeypatch,
        {
            "summary_batch_1": draft.replace("draft context", "x" * 1200),
            "summary_compress": response_text,
        },
        max_chars=1000,
    )
    assert result["raw_summary"] == final
    assert calls == ["summary_batch_1", "summary_compress"]


@pytest.mark.asyncio
async def test_compression_accepts_unlabelled_fenced_xml(monkeypatch):
    final = valid_summary_xml_for_tests(seed="real context")

    async def response(**_kwargs):
        return "</think>\n```xml\n" + final + "\n```"

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
        '\nThe log displayed "</think>".',
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
