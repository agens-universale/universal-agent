"""Резолв путей к фиксированным prompts-ассетам TUI через единый bundled_root()."""

import os

from gc_core.bundled_assets import bundled_root


def tui_special_instructions_path() -> str:
    """Абсолютный путь к prompts/chat_completions_tui_special_instructions.md.
    Фиксированный product-asset, не config-overridable."""
    return os.path.join(
        str(bundled_root()),
        "prompts",
        "chat_completions_tui_special_instructions.md",
    )
