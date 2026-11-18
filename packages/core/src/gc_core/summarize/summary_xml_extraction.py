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
_ROOT_FIRST_SECTION = re.compile(rf"\s*<(?:{_SECTIONS})>")
_FENCED_CODE = re.compile(
    r"(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*(?=\n|$)"
)
_INLINE_CODE = re.compile(r"(?<![\\`])(`+)(?!`)[^`\n]*\1(?!`)")
_SUMMARY_OPEN = "<summary>"
_SUMMARY_CLOSE = "</summary>"
_THINK_CLOSE = "</think>"
_OUTER_TOKENS = {_SUMMARY_OPEN, _SUMMARY_CLOSE, _THINK_CLOSE}


def _outside_cdata(source: str):
    for match in _TOKEN.finditer(source):
        if not match.group().startswith("<![CDATA["):
            yield match


def _closed_section_spans(source: str) -> list[tuple[int, int]]:
    openings = {tag: [] for tag in section_tags()}
    spans: list[tuple[int, int]] = []
    for match in _outside_cdata(source):
        token = match.group()
        closing = token.startswith("</")
        tag = token[2:-1] if closing else token[1:-1]
        if tag not in openings:
            continue
        if closing:
            if openings[tag]:
                spans.append((openings[tag].pop(), match.end()))
        else:
            openings[tag].append(match.start())
    return spans


def _outside_closed_sections(source: str):
    spans = _closed_section_spans(source)
    for match in _outside_cdata(source):
        if not any(start <= match.start() < end for start, end in spans):
            yield match


def _reasoning_boundaries(source: str) -> list[int]:
    literal_spans = [
        match.span()
        for pattern in (_FENCED_CODE, _INLINE_CODE)
        for match in pattern.finditer(source)
    ]
    return [
        match.end()
        for match in _outside_closed_sections(source)
        if match.group() == _THINK_CLOSE
        and not any(
            start <= match.start() < end for start, end in literal_spans
        )
    ]


def _candidate_tokens(source: str):
    for match in _outside_closed_sections(source):
        if match.group() == _SUMMARY_OPEN and not _ROOT_FIRST_SECTION.match(
            source, match.end()
        ):
            continue
        yield match


def _outer_tokens(source: str) -> list[re.Match[str]]:
    """Ignore section text and CDATA while locating the summary envelope."""
    tokens: list[re.Match[str]] = []
    in_summary = False
    for match in _candidate_tokens(source):
        token = match.group()
        if token not in _OUTER_TOKENS:
            continue
        if token == _SUMMARY_CLOSE and not in_summary:
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
    boundaries = _reasoning_boundaries(source)
    if boundaries:
        candidate = _single_summary(source, boundaries[-1])
        if (
            candidate is not None
            and validate_summary_text(
                candidate, require_closed_structure=True
            )[0]
        ):
            return candidate
        raise SummaryRejectedError(
            "summary produced no usable summary after reasoning boundary"
        )
    return _single_summary(source, 0)
