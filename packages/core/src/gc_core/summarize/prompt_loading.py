"""Промпты суммаризации и memory-patch: чтение из хранилища агента и
подстановки."""

from gc_core.agent_store import AgentStore, Prompt, aread_text
from gc_core.summarize.summary_spec import (
    MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER,
    SUMMARY_OUTPUT_SHAPE_PLACEHOLDER,
    SUMMARY_SECTIONS_PLACEHOLDER,
    render_output_shape_for_prompt,
    render_sections_for_prompt,
    render_summary_tags_for_memory_patch_prompt,
)
from gc_core.summarize.working_memory_gateway import (
    inject_working_memory_prompt_placeholders,
)


async def aload_summary_prompt(store: AgentStore) -> str:
    """Системный промпт суммаризации из хранилища агента (у файлового —
    ``summary.chat_completions_summary_prompt_path`` или пакетный)."""
    # Путь задаёт КОНФИГ — файл может быть чей угодно и в любой кодировке.
    # Отказ обязан назвать его: без пути человек читает голую жалобу кодека и
    # не знает, какой из промптов чинить.
    raw = await aread_text(store, "prompt", Prompt.SUMMARY)
    return render_summary_prompt(raw, label=store.label("prompt", "summary"))


def render_summary_prompt(raw: str, *, label: str) -> str:
    """Текст промпта суммаризации с подстановками; пустой — ``ValueError``
    (иначе LLM получил бы пустой system prompt)."""
    prompt = raw.strip()
    if not prompt:
        raise ValueError(f"Summary prompt file is empty: {label}")
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


MEMORY_PATCH_SUBAGENT_GUIDANCE_PLACEHOLDER = (
    "{{MEMORY_PATCH_SUBAGENT_GUIDANCE}}"
)

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


async def aload_memory_patch_prompt(
    store: AgentStore, *, subagent: bool = False
) -> str:
    """Промпт memory-patch из хранилища агента (у файлового —
    ``summary.chat_completions_memory_patch_replace_prompt_path`` или
    пакетный)."""
    raw = await aread_text(store, "prompt", Prompt.MEMORY_PATCH)
    return render_memory_patch_prompt(
        raw, label=store.label("prompt", "memory_patch"), subagent=subagent
    )


def render_memory_patch_prompt(
    raw: str, *, label: str, subagent: bool = False
) -> str:
    """Текст промпта memory-patch с подстановками; пустой — ``ValueError``."""
    prompt = raw.strip()
    if not prompt:
        raise ValueError(f"Memory patch prompt file is empty: {label}")
    if MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER in prompt:
        prompt = prompt.replace(
            MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER,
            render_summary_tags_for_memory_patch_prompt(),
        )
    if MEMORY_PATCH_SUBAGENT_GUIDANCE_PLACEHOLDER in prompt:
        guidance = MEMORY_PATCH_SUBAGENT_GUIDANCE_TEXT if subagent else ""
        prompt = prompt.replace(
            MEMORY_PATCH_SUBAGENT_GUIDANCE_PLACEHOLDER, guidance
        )
    prompt = inject_working_memory_prompt_placeholders(prompt)
    # Drop blank lines left when subagent guidance is empty.
    while "\n\n\n" in prompt:
        prompt = prompt.replace("\n\n\n", "\n\n")
    return prompt.strip()
