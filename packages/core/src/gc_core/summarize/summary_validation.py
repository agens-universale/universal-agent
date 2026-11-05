"""Проверка XML-ответа summarizer."""

from __future__ import annotations

from typing import Any

from gc_core.summarize.summary_spec import (
    assemble_summary_xml,
    extract_summary_bodies,
    merge_summary_bodies,
    section_tags,
)

_PLACEHOLDER_LINES = {"", "...", "…", "n/a", "- n/a"}


class SummaryRejectedError(ValueError):
    """A summary failed validation before any change to the live history."""

    trace: dict[str, Any] | None = None


def has_nonempty_summary_body(text: str) -> bool:
    """Return whether a known section contains more than placeholders."""
    return any(
        any(
            line.strip().casefold() not in _PLACEHOLDER_LINES
            for line in body.splitlines()
        )
        for body in extract_summary_bodies(text).values()
    )


def missing_section_tags(
    text: str, *, allow_unclosed: bool = True
) -> tuple[str, ...]:
    bodies = extract_summary_bodies(text, allow_unclosed=allow_unclosed)
    return tuple(tag for tag in section_tags() if tag not in bodies)


def validate_summary_text(
    text: str, *, require_closed_structure: bool = False
) -> tuple[bool, str]:
    s = (text or "").strip()
    if not s:
        return False, "empty"
    if require_closed_structure and not (
        s.startswith("<summary>") and s.endswith("</summary>")
    ):
        return False, "missing_structure"
    if missing_section_tags(s, allow_unclosed=not require_closed_structure):
        return False, "missing_structure"
    if not has_nonempty_summary_body(s):
        return False, "empty_content"
    return True, "ok"


def merge_repaired_summary(*, invalid_text: str, repaired_text: str) -> str:
    """Склеивает теги из repair поверх сохранённых из невалидного черновика.

    Ожидает already-projected тексты (aux LLM choke в ``request_summary_text``).
    """
    preserved = extract_summary_bodies(invalid_text)
    repaired = extract_summary_bodies(repaired_text)
    return assemble_summary_xml(
        merge_summary_bodies(preserved=preserved, repaired=repaired)
    )
