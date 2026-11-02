"""Проверка XML-ответа summarizer."""

from __future__ import annotations

from gc_core.summarize.summary_spec import (
    assemble_summary_xml,
    extract_summary_bodies,
    merge_summary_bodies,
    primary_section_tag,
    section_tags,
)

_FALLBACK_MSG = (
    "Automatic summarization produced no valid output. "
    "Continue from the latest confirmed user requirements and implementation state."
)


def has_nonempty_summary_body(text: str) -> bool:
    """Return whether a known section has content beyond template ellipses."""
    return any(
        body.strip()
        and any(
            line.strip() not in {"", "...", "…"} for line in body.splitlines()
        )
        for body in extract_summary_bodies(text).values()
    )


def fallback_summary_xml() -> str:
    filler = "- n/a"
    bodies = dict.fromkeys(section_tags(), filler)
    bodies[primary_section_tag()] = (
        f"- {_FALLBACK_MSG}\n- Continue from dialog_history and session memory."
    )
    return assemble_summary_xml(bodies)


# Для импортов; всегда валидный минимальный <summary>.
FALLBACK_SUMMARY = fallback_summary_xml()


def missing_section_tags(text: str) -> tuple[str, ...]:
    bodies = extract_summary_bodies(text)
    return tuple(tag for tag in section_tags() if tag not in bodies)


def validate_summary_text(text: str) -> tuple[bool, str]:
    s = (text or "").strip()
    if not s:
        return False, "empty"
    if missing_section_tags(s):
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


def resolve_after_repair_failure(
    *,
    merged_text: str,
) -> tuple[str, str]:
    """Keep a useful partial repair; otherwise leave source history intact."""
    if has_nonempty_summary_body(merged_text):
        return merged_text, "partial_merged"
    raise ValueError("summary repair produced no usable summary")
