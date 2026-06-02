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


# Eight sections: rolling handoff only; durable goal/constraints live in persistent_state (WM).
SUMMARY_SECTIONS: tuple[SummarySection, ...] = (
    SummarySection(
        title="TASK_AND_CONTEXT",
        description=(
            "- Current task, scope, and what is being solved now.\n"
            "- Active goals/subgoals for the next few iterations (do not repeat stable goal text already in persistent_state unless urgent).\n"
            "- Blockers, open questions, and key assumptions that affect the next steps (brief; use `n/a` if none)."
        ),
    ),
    SummarySection(
        title="EXECUTION_STAGE",
        description=(
            "- Must be one of:\n"
            "  planning | execution\n"
            "- If `planning`, explicitly state `planning-only`.\n"
            "- If `execution`, concrete task-solving actions may continue:\n"
            "  coding, drafting, editing, tool use, analysis, research, planning artifact creation, or other execution work.\n"
            "- All later sections, especially NEXT_ACTIONS, must respect this stage."
        ),
    ),
    SummarySection(
        title="RECENT_PROGRESS",
        description=(
            "- Brief progress with statuses: [DONE], [IN_PROGRESS], [BLOCKED], [TODO].\n"
            "- Current approach, hypothesis, draft status, or local execution context for the next 1-3 turns "
            "(temporary detail not yet in persistent_state).\n"
            "- Concrete outcomes and exact references where relevant; no full transcript.\n"
            "- Must respect EXECUTION_STAGE."
        ),
    ),
    SummarySection(
        title="ACTIVE_SKILL",
        description=(
            "- One canonical skill_id for the workflow the next assistant should continue **right now** "
            "(the skill the session is actually running on, not every skill ever mentioned).\n"
            "- Derive it from: the latest relevant `skills_load` in dialog_history or dialog_history_batch; "
            "or, if `<previous_summary>` had ACTIVE_SKILL and the task/workflow is unchanged, carry that id forward.\n"
            "- Replace or clear the id when the user switches tasks, abandons the skill workflow, or it is clearly finished; then use `n/a`.\n"
            "- Do not invent skill ids. The catalog stays in system context; this line tells which id to `skills_load` again when the body is still needed.\n"
            "- If not `n/a`, NEXT_ACTIONS may include `skills_load` for that id when execution continues that workflow."
        ),
    ),
    SummarySection(
        title="NEXT_ACTIONS",
        description=(
            "- 3-7 focused, executable next actions for the next 1-3 iterations.\n"
            "- If EXECUTION_STAGE is `planning`, next actions must remain planning, analysis, review, or clarification only.\n"
            "- If EXECUTION_STAGE is `execution`, next actions may include concrete task-solving, tool use, drafting, editing, coding, or research work.\n"
            "- Must not repeat items listed under DO_NOT_REDO unless the user explicitly requests a retry."
        ),
    ),
    SummarySection(
        title="DO_NOT_REDO",
        description=(
            "- Approaches, edits, commands, configs, or fixes already tried and rejected, failed, or superseded.\n"
            "- Include user corrections that ruled something out (\"don't use X\", \"revert Y\", \"that path failed\").\n"
            "- Include dead ends from tool errors or verification failures when they should not be retried blindly.\n"
            "- Use `n/a` only if nothing was explicitly ruled out or abandoned.\n"
            "- Do not invent rejected attempts; every item must be traceable to dialogue, tool output, or user message."
        ),
    ),
    SummarySection(
        title="SPEC_AND_CONSTRAINTS",
        description=(
            "- Requirements, contracts, compatibility, acceptance criteria, or preferences **still binding** for continuation.\n"
            "- Only what is missing from persistent_state or newly changed; do not copy stable constraints already there.\n"
            "- Use `n/a` if persistent_state already covers constraints for the next steps."
        ),
    ),
    SummarySection(
        title="RECENT_ARTIFACTS_AND_SOURCES",
        description=(
            "- Recent files, documents, URLs, sources, modules, symbols, tests, commands, endpoints, drafts, outputs, tools, environment details, claims, or evidence.\n"
            "- Prefer details that are costly to rediscover.\n"
            "- Every artifact/source/path/reference must be traceable to explicit dialogue, tool output, user message, or provided context.\n"
            "- Do not invent paths, URLs, citations, sources, or artifacts."
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
