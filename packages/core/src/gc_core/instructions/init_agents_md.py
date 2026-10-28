from __future__ import annotations

from pathlib import Path

from gc_core.agent_store import AgentStore, Prompt, aread_required

_BLOCK_NEW = "There is no AGENTS.md yet — create it from scratch."
_BLOCK_ASK = (
    "An AGENTS.md already exists. First read it. Then ask the user via the ask_human tool "
    "whether to UPDATE it (refresh and augment, preserving their manual edits and structure) "
    "or RECREATE it from scratch. Act on their answer. Never silently discard the user's "
    "manual edits."
)
_BLOCK_HEADLESS = (
    "An AGENTS.md already exists. Read it and UPDATE it in place — refresh and augment, "
    "preserving manual edits and structure. Do NOT recreate from scratch, and do NOT ask."
)


async def abuild_init_prompt(
    *, store: AgentStore, cwd: str, can_ask: bool
) -> str:
    """Собрать init-промпт: агент сам сканирует репо и пишет AGENTS.md через write_to_file.

    Шаблон — промпт хранилища агента (``init_agents_md``). Ветка по
    существующему файлу зашивается здесь (вызывающая сторона передаёт
    can_ask = доступность ask_human/confirm), чтобы модель не рассуждала о
    доступности тулов.
    """
    label = store.label("prompt", Prompt.INIT_AGENTS_MD)
    template = await aread_required(
        store,
        "prompt",
        Prompt.INIT_AGENTS_MD,
        missing=f"Init AGENTS.md prompt not found: {label}",
    )
    exists = (Path(cwd) / "AGENTS.md").exists()
    if not exists:
        block = _BLOCK_NEW
    elif can_ask:
        block = _BLOCK_ASK
    else:
        block = _BLOCK_HEADLESS
    return template.replace("{existing_block}", block)
