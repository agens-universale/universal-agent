"""Канон секций dialog summary: один источник для промпта и валидации."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# Подставляется в `prompts/chat_completions_summary_prompt.md` при загрузке.
SUMMARY_SECTIONS_PLACEHOLDER = "{{SUMMARY_SECTIONS}}"

# Минимум секций из ``SUMMARY_SECTIONS``, которые должны «попасть» в ответ (эвристика по ключевым словам).
MIN_SECTION_HITS = 3


@dataclass(frozen=True)
class SummarySection:
    """Одна обязательная секция структурированной сводки диалога."""

    title: str
    """Заголовок в формате UPPERCASE_WITH_UNDERSCORES (как в ответе модели)."""
    description: str
    """Тело секции для system prompt: строки с ``- `` bullets."""
    required: bool = True
    """Если True — ``validate_summary_text`` требует наличие секции в ответе."""


# Six sections: compact rolling handoff; durable goal/constraints/facts live in persistent_state (WM).
SUMMARY_SECTIONS: tuple[SummarySection, ...] = (
    SummarySection(
        title="TASK_AND_CONTEXT",
        description=(
            "- Current task, scope, and what is being solved now.\n"
            "- Active goals/subgoals for the next few iterations (brief).\n"
            "- Blockers, open questions, and key assumptions that affect the next steps (`n/a` if none)."
        ),
    ),
    SummarySection(
        title="OPEN_TASKS",
        description=(
            "- First line: `mode: planning-only` or `mode: execution` (from explicit user intent; if unclear, `planning-only`).\n"
            "- Optional second line: `skill: <skill_id>` or `skill: n/a` (workflow to continue now; from latest `skills_load` or previous summary if unchanged).\n"
            "- 3-7 open work items with statuses [TODO], [IN_PROGRESS], or [BLOCKED] for the next 1-3 iterations (no [DONE] here).\n"
            "- If the current user turn/batch is finished with no blocker, use `- none` or one idle line — no hypothetical future [TODO].\n"
            "- Brief completed outcomes from this batch belong in LAST_VERIFIED_FACTS, not here.\n"
            "- If `planning-only`, tasks are analysis, review, clarification, or planning artifacts only (no implementation edits unless user approved).\n"
            "- If `execution`, concrete solving may continue (tools, edits, drafting, coding, research)."
        ),
    ),
    SummarySection(
        title="DO_NOT_REDO",
        description=(
            "- Approaches, edits, commands, configs, or fixes already tried and rejected, failed, or superseded.\n"
            "- User corrections that ruled something out (\"don't use X\", \"revert Y\", \"that path failed\").\n"
            "- Dead ends from tool errors or verification failures when they should not be retried blindly.\n"
            "- Use `n/a` only if nothing was explicitly ruled out or abandoned.\n"
            "- Do not invent rejected attempts; every item must be traceable to dialogue, tool output, or user message."
        ),
    ),
    SummarySection(
        title="LAST_VERIFIED_FACTS",
        description=(
            "- **This batch only:** new or changed verified outcomes (command → verdict, or path → what was proven).\n"
            "- Do **not** repeat sha256/checksum catalogs, path lists, or facts already in `<persistent_state>` unless corrected this batch.\n"
            "- Prefer `n/a` if nothing new was verified in this batch.\n"
            "- Do not invent paths, numbers, or outcomes; trace every item to dialogue or tool output."
        ),
    ),
    SummarySection(
        title="ACTIVE_CONSTRAINTS",
        description=(
            "- Requirements, compatibility, acceptance criteria, or preferences still binding for continuation.\n"
            "- Use `n/a` if none beyond what the session already established."
        ),
    ),
    SummarySection(
        title="RECENT_ARTIFACTS",
        description=(
            "- Recent paths, URLs, modules, symbols, drafts, or outputs as `path -> purpose` (index only, not file contents).\n"
            "- No checksum/sha256 lines here (session memory `knowledge` is canonical for those).\n"
            "- Do not duplicate LAST_VERIFIED_FACTS; artifacts answer *where*, facts answer *what was proven*.\n"
            "- Use `n/a` if unchanged since persistent_state; every entry traceable to dialogue, tool output, or user message."
        ),
    ),
)


def required_sections() -> tuple[SummarySection, ...]:
    return tuple(s for s in SUMMARY_SECTIONS if s.required)


def section_titles(*, required_only: bool = True) -> tuple[str, ...]:
    sections = required_sections() if required_only else SUMMARY_SECTIONS
    return tuple(s.title for s in sections)


def render_sections_for_prompt(sections: Sequence[SummarySection] | None = None) -> str:
    """Блок секций для system prompt (после «Required structure»)."""
    items = list(sections) if sections is not None else list(SUMMARY_SECTIONS)
    return "\n\n".join(f"{s.title}:\n{s.description.strip()}" for s in items)


def format_section_titles_for_repair() -> str:
    """Список заголовков для user prompt repair (через запятую)."""
    return ", ".join(section_titles())
