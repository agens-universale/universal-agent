from __future__ import annotations

from pathlib import Path

from gc_core.bundled_assets import bundled_root

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


def _prompt_template() -> str:
    return (bundled_root() / "prompts" / "init_agents_md_prompt.md").read_text(encoding="utf-8")


def build_init_prompt(*, cwd: str, can_ask: bool) -> str:
    """Собрать init-промпт: агент сам сканирует репо и пишет AGENTS.md через write_to_file.

    Ветка по существующему файлу зашивается здесь (вызывающая сторона передаёт can_ask
    = доступность ask_human/confirm), чтобы модель не рассуждала о доступности тулов.
    """
    exists = (Path(cwd) / "AGENTS.md").exists()
    if not exists:
        block = _BLOCK_NEW
    elif can_ask:
        block = _BLOCK_ASK
    else:
        block = _BLOCK_HEADLESS
    return _prompt_template().replace("{existing_block}", block)
