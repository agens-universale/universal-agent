"""Канон секций dialog summary: промпт, валидация, slice."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

SUMMARY_SECTIONS_PLACEHOLDER = "{{SUMMARY_SECTIONS}}"
SUMMARY_OUTPUT_SHAPE_PLACEHOLDER = "{{SUMMARY_OUTPUT_SHAPE}}"

MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER = "{{MEMORY_PATCH_SUMMARY_TAGS}}"

SummarySliceMode = Literal["short", "full"]


@dataclass(frozen=True)
class SummarySection:
    tag: str
    agent_visible: bool
    memory_patch_hint: str
    description: str


SUMMARY_SECTIONS: tuple[SummarySection, ...] = (
    SummarySection(
        tag="task_and_context",
        agent_visible=True,
        memory_patch_hint="align `<goal>` / `<state>`; do not copy verbatim into multiple registers",
        description=(
            "- Current task, scope, and what is being solved now.\n"
            "- Active goals/subgoals for the next few iterations (brief).\n"
            "- Blockers, open questions, and key assumptions that affect the next steps (`n/a` if none)."
        ),
    ),
    SummarySection(
        tag="open_tasks",
        agent_visible=True,
        memory_patch_hint="align `<state>` (open work only); no [DONE] or completed steps",
        description=(
            "- First line: `mode: planning-only` or `mode: execution` (from explicit user intent; if unclear, `planning-only`).\n"
            "- Optional second line: `skill: <skill_id>` or `skill: n/a` (workflow to continue now; from latest `skills_load` or previous summary if unchanged).\n"
            "- 3-7 open work items with statuses [TODO], [IN_PROGRESS], or [BLOCKED] for the next 1-3 iterations (no [DONE] here).\n"
            "- If the current user turn/batch is finished with no blocker, use `- none` or one idle line — no hypothetical future [TODO].\n"
            "- Brief completed outcomes from this batch belong in last_verified_facts, not here.\n"
            "- If `planning-only`, tasks are analysis, review, clarification, or planning artifacts only (no implementation edits unless user approved).\n"
            "- If `execution`, concrete solving may continue (tools, edits, drafting, coding, research)."
        ),
    ),
    SummarySection(
        tag="do_not_redo",
        agent_visible=True,
        memory_patch_hint="usually stays in summary only; do not copy into registers",
        description=(
            "- Approaches, edits, commands, configs, or fixes already tried and rejected, failed, or superseded.\n"
            '- User corrections that ruled something out ("don\'t use X", "revert Y", "that path failed").\n'
            "- Dead ends from tool errors or verification failures when they should not be retried blindly.\n"
            "- Use `n/a` only if nothing was explicitly ruled out or abandoned.\n"
            "- Do not invent rejected attempts; every item must be traceable to dialogue, tool output, or user message."
        ),
    ),
    SummarySection(
        tag="last_verified_facts",
        agent_visible=True,
        memory_patch_hint="batch deltas → merge into `<knowledge>`",
        description=(
            "- **This batch only:** new or changed verified outcomes (command → verdict, or path → what was proven).\n"
            "- File/module refs: full absolute paths as in tool results (`.../src/backends/local.py:84`), never basename-only (`local.py`).\n"
            "- Do **not** repeat sha256/checksum catalogs, path lists, or facts already in `<persistent_state>` unless corrected this batch.\n"
            "- Prefer `n/a` if nothing new was verified in this batch.\n"
            "- Do not invent paths, numbers, or outcomes; trace every item to dialogue or tool output."
        ),
    ),
    SummarySection(
        tag="active_constraints",
        agent_visible=False,
        memory_patch_hint="binding requirements → merge into `<constraints>`",
        description=(
            "- Requirements, compatibility, acceptance criteria, or preferences still binding for continuation.\n"
            "- Use `n/a` if none beyond what the session already established."
        ),
    ),
    SummarySection(
        tag="recent_artifacts",
        agent_visible=True,
        memory_patch_hint="path → purpose index (no sha256) → merge into `<knowledge>`",
        description=(
            "- Recent paths, URLs, modules, symbols, drafts, or outputs as `path -> purpose` (index only, not file contents).\n"
            "- Paths must be full absolute paths as in tool results (`.../src/tools/fs/file_info.py`), never basename-only.\n"
            "- No checksum/sha256 lines here (session memory `knowledge` is canonical for those).\n"
            "- Do not duplicate last_verified_facts; artifacts answer *where*, facts answer *what was proven*.\n"
            "- Use `n/a` if unchanged since persistent_state; every entry traceable to dialogue, tool output, or user message."
        ),
    ),
)


def section_tags() -> tuple[str, ...]:
    return tuple(s.tag for s in SUMMARY_SECTIONS)


def agent_visible_section_tags() -> tuple[str, ...]:
    return tuple(s.tag for s in SUMMARY_SECTIONS if s.agent_visible)


def hint_section_tags() -> tuple[str, ...]:
    return tuple(s.tag for s in SUMMARY_SECTIONS if not s.agent_visible)


def primary_section_tag() -> str:
    """Первая секция в каноне (fallback-текст в `fallback_summary_xml`)."""
    return SUMMARY_SECTIONS[0].tag


def _build_section_body_patterns() -> dict[str, re.Pattern[str]]:
    """Regex по одному на каждый `SUMMARY_SECTIONS[].tag` — единственный whitelist для extract."""
    return {
        section.tag: re.compile(
            rf"<{re.escape(section.tag)}>\s*(.*?)\s*</{re.escape(section.tag)}>",
            re.DOTALL,
        )
        for section in SUMMARY_SECTIONS
    }


_SECTION_BODY_PATTERNS = _build_section_body_patterns()


def render_sections_for_prompt() -> str:
    return "\n\n".join(
        f"<{s.tag}>\n{s.description.strip()}\n</{s.tag}>"
        for s in SUMMARY_SECTIONS
    )


def render_output_shape_for_prompt() -> str:
    lines = ["<summary>"]
    for tag in section_tags():
        lines.append(f"  <{tag}>...</{tag}>")
    lines.append("</summary>")
    return "\n".join(lines)


def render_summary_tags_for_memory_patch_prompt() -> str:
    return "\n".join(
        f"- `{s.tag}` — {s.memory_patch_hint}" for s in SUMMARY_SECTIONS
    )


def render_tag_roles_for_prompt() -> str:
    return "\n".join(
        f"- {s.tag}: {s.description.strip().splitlines()[0].lstrip('- ')}"
        for s in SUMMARY_SECTIONS
    )


def format_section_tags_for_repair() -> str:
    return ", ".join(section_tags())


def extract_summary_bodies(text: str) -> dict[str, str]:
    """Вырезает тела известных секций regex ``<tag>…</tag>`` (последний match); пустое тело → ``''``."""
    s = (text or "").strip()
    if not s:
        return {}
    bodies: dict[str, str] = {}
    for section in SUMMARY_SECTIONS:
        pattern = _SECTION_BODY_PATTERNS[section.tag]
        last_match: re.Match[str] | None = None
        for match in pattern.finditer(s):
            last_match = match
        if last_match is not None:
            bodies[section.tag] = last_match.group(1).strip()
    return bodies


def assemble_summary_xml(
    bodies: dict[str, str], *, only_present: bool = False
) -> str:
    """Собрать `<summary>`. ``only_present=True`` — только ключи из ``bodies`` (ответ repair с частью тегов)."""
    lines = ["<summary>"]
    for section in SUMMARY_SECTIONS:
        if only_present and section.tag not in bodies:
            continue
        body = (bodies.get(section.tag) or "").strip()
        lines.append(f"  <{section.tag}>{body}</{section.tag}>")
    lines.append("</summary>")
    return "\n".join(lines)


def merge_summary_bodies(
    *, preserved: dict[str, str], repaired: dict[str, str]
) -> dict[str, str]:
    """Repair перекрывает preserved; тег в repair с пустым телом затирает секцию; нет в repair — preserved."""
    merged: dict[str, str] = {}
    for section in SUMMARY_SECTIONS:
        tag = section.tag
        if tag in repaired:
            merged[tag] = (repaired[tag] or "").strip()
        elif tag in preserved:
            merged[tag] = (preserved[tag] or "").strip()
        else:
            merged[tag] = ""
    return merged


def agent_summary_slice_mode(
    *, num_batches: int, patch_executed: bool
) -> SummarySliceMode:
    if num_batches <= 1 and patch_executed:
        return "short"
    return "full"


def slice_summary(xml_text: str, mode: SummarySliceMode) -> str:
    bodies = extract_summary_bodies(xml_text)
    if not bodies:
        raise ValueError("expected <summary> content")
    tags = [
        s.tag for s in SUMMARY_SECTIONS if mode == "full" or s.agent_visible
    ]
    sliced = {tag: bodies[tag] for tag in tags if tag in bodies}
    if not sliced:
        raise ValueError("expected <summary> content")
    return assemble_summary_xml(sliced, only_present=True)
