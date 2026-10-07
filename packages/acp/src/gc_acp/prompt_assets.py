"""Резолв prompts-ассетов ACP через bundled_root()."""

from __future__ import annotations

import os

from gc_core.bundled_assets import bundled_root


def acp_special_instructions_path() -> str:
    """Абсолютный путь к prompts/chat_completions_acp_special_instructions.md."""
    return os.path.join(
        str(bundled_root()),
        "prompts",
        "chat_completions_acp_special_instructions.md",
    )
