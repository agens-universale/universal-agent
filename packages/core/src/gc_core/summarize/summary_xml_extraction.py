"""Isolate one final summary document from auxiliary model output."""

import re

from gc_core.summarize.summary_spec import section_tags
from gc_core.summarize.summary_validation import (
    SummaryRejectedError,
    validate_summary_text,
)

_SECTIONS = "|".join(re.escape(tag) for tag in section_tags())
_TOKEN = re.compile(
    rf"<!\[CDATA\[.*?\]\]>|</think>|</?summary>|</?(?:{_SECTIONS})>",
    re.DOTALL,
)
_SUMMARY_OPEN = "<summary>"
_SUMMARY_CLOSE = "</summary>"
_THINK_CLOSE = "</think>"
_OUTER_TOKENS = {_SUMMARY_OPEN, _SUMMARY_CLOSE, _THINK_CLOSE}


def _outside_cdata(source: str):
    for match in _TOKEN.finditer(source):
        if not match.group().startswith("<![CDATA["):
            yield match


def _outer_tokens(source: str) -> list[re.Match[str]]:
    """Ignore section text and CDATA while locating the summary envelope."""
    tokens: list[re.Match[str]] = []
    in_summary = False
    active_section: str | None = None
    for match in _outside_cdata(source):
        token = match.group()
        if active_section is not None:
            if token == f"</{active_section}>":
                active_section = None
            continue
        if in_summary and token not in _OUTER_TOKENS:
            if not token.startswith("</"):
                active_section = token[1:-1]
            continue
        tokens.append(match)
        if token == _SUMMARY_OPEN:
            in_summary = True
        elif token == _SUMMARY_CLOSE:
            in_summary = False
    return tokens


def _single_summary(source: str, boundary: int) -> str | None:
    answer_tokens = _outer_tokens(source[boundary:])
    opens = [
        match for match in answer_tokens if match.group() == _SUMMARY_OPEN
    ]
    closes = [
        match for match in answer_tokens if match.group() == _SUMMARY_CLOSE
    ]
    if len(opens) > 1 or len(closes) > 1:
        raise SummaryRejectedError(
            "summary produced no usable summary (ambiguous final XML)"
        )
    if len(opens) != 1 or len(closes) != 1:
        return None
    if opens[0].start() >= closes[0].start():
        return None
    return source[boundary + opens[0].start() : boundary + closes[0].end()]


def extract_final_summary_xml(text: str) -> str | None:
    """Return one final summary, keeping partial drafts for the repair path."""
    source = text or ""
    boundaries = [
        match.end()
        for match in _outside_cdata(source)
        if match.group() == _THINK_CLOSE
    ]
    for boundary in reversed(boundaries):
        if any(
            match.group() == _SUMMARY_OPEN
            for match in _outer_tokens(source[boundary:])
        ):
            candidate = _single_summary(source, boundary)
            if (
                candidate is not None
                and validate_summary_text(
                    candidate, require_closed_structure=True
                )[0]
            ):
                return candidate
    return _single_summary(source, 0)
