from xml.etree import ElementTree

import pytest
from gc_core.summarize.summary_llm_steps import acompress_summary_text
from gc_core.summarize.summary_spec import assemble_summary_xml
from gc_core.summarize.summary_validation import SummaryRejectedError
from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests


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
    final = final.replace("plain", "plain 3 < 4 & <summary> is a label")

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
@pytest.mark.parametrize(
    "response_text",
    [
        "<summary><task_and_context>unfinished",
        valid_summary_xml_for_tests(seed="first")
        + "\n"
        + valid_summary_xml_for_tests(seed="second"),
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
