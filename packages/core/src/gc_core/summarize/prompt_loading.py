"""Путь к файлу промпта суммаризации по умолчанию и чтение шаблона."""

import os

from gc_core.bundled_assets import bundled_root
from gc_core.summarize.summary_spec import (
    MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER,
    SUMMARY_OUTPUT_SHAPE_PLACEHOLDER,
    SUMMARY_SECTIONS_PLACEHOLDER,
    render_output_shape_for_prompt,
    render_sections_for_prompt,
    render_summary_tags_for_memory_patch_prompt,
)
from gc_core.summarize.working_memory_gateway import inject_working_memory_prompt_placeholders
from gc_core.utils.user_text import read_user_text


def default_prompt_path() -> str:
    """Возвращает путь к файлу системного промпта суммаризации по умолчанию.

    Назначение: единая точка, откуда оркестратор суммаризации берёт шаблон
    инструкций для LLM, если в конфиге не задан свой путь.

    Returns:
        Абсолютный путь к `prompts/chat_completions_summary_prompt.md` в корне репозитория.
    """
    return os.path.join(str(bundled_root()), "prompts", "chat_completions_summary_prompt.md")


def load_prompt_template(path: str) -> str:
    """Читает файл с системным промптом суммаризации и проверяет, что он не пустой.

    Args:
        path: Путь к markdown/текстовому файлу с промптом.

    Returns:
        Содержимое файла без ведущих/конечных пробельных строк.

    Raises:
        ValueError: Если после `strip()` текст пустой — иначе LLM получил бы
            пустой system prompt.
    """
    # Путь задаёт КОНФИГ (`summary_prompt_path`) — файл может быть чей угодно и в
    # любой кодировке. Отказ обязан назвать его: без пути человек читает голую
    # жалобу кодека и не знает, какой из промптов чинить.
    prompt = read_user_text(path).strip()
    if not prompt:
        raise ValueError(f"Summary prompt file is empty: {path}")
    if SUMMARY_SECTIONS_PLACEHOLDER in prompt:
        prompt = prompt.replace(
            SUMMARY_SECTIONS_PLACEHOLDER,
            render_sections_for_prompt(),
        )
    if SUMMARY_OUTPUT_SHAPE_PLACEHOLDER in prompt:
        prompt = prompt.replace(
            SUMMARY_OUTPUT_SHAPE_PLACEHOLDER,
            render_output_shape_for_prompt(),
        )
    return inject_working_memory_prompt_placeholders(prompt)


MEMORY_PATCH_SUBAGENT_GUIDANCE_PLACEHOLDER = "{{MEMORY_PATCH_SUBAGENT_GUIDANCE}}"

# Soft-only: no hard filter on apply. Injected when patch runs on a subagent State
# (shared working_memory with the orchestrator).
MEMORY_PATCH_SUBAGENT_GUIDANCE_TEXT = """## Subagent run (shared thread memory)

This compaction is for a **subagent** child dialog. Session memory registers are
**shared with the orchestrator** for the whole thread — not a private child scratchpad.

- Treat `<persistent_state>` as the orchestrator baseline. Do **not** rewrite
  `<goal>` or `<constraints>` to match only the subagent assignment when the
  baseline already holds the thread-level goal/constraints.
- Do **not** replace orchestrator `<state>` (open plan / TODOs) with the
  subagent's local checklist. Keep orchestrator open work; add at most a short
  durable thread-level fact or blocker if the dialog clearly establishes one.
- Prefer durable updates in `<knowledge>` (verified facts, artifact paths,
  decisions worth keeping after the child ends).
- If there is nothing durable to add, **repeat** baseline registers as-is —
  do not "simplify" or re-scope memory around the child task alone.
- Subagent assignment text in dialog/summary is local work context, not a
  license to clobber orchestrator goal/state.
""".strip()


def _load_memory_patch_prompt(path: str, *, subagent: bool = False) -> str:
    prompt = read_user_text(path).strip()
    if not prompt:
        raise ValueError(f"Memory patch prompt file is empty: {path}")
    if MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER in prompt:
        prompt = prompt.replace(
            MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER,
            render_summary_tags_for_memory_patch_prompt(),
        )
    if MEMORY_PATCH_SUBAGENT_GUIDANCE_PLACEHOLDER in prompt:
        guidance = MEMORY_PATCH_SUBAGENT_GUIDANCE_TEXT if subagent else ""
        prompt = prompt.replace(MEMORY_PATCH_SUBAGENT_GUIDANCE_PLACEHOLDER, guidance)
    prompt = inject_working_memory_prompt_placeholders(prompt)
    # Drop blank lines left when subagent guidance is empty.
    while "\n\n\n" in prompt:
        prompt = prompt.replace("\n\n\n", "\n\n")
    return prompt.strip()


def load_memory_patch_replace_prompt(path: str, *, subagent: bool = False) -> str:
    return _load_memory_patch_prompt(path, subagent=subagent)
