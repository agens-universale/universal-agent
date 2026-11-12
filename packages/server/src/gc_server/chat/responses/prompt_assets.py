"""Резолв prompts-ассетов Responses через bundled_root()."""

from __future__ import annotations

import os

from gc_core.bundled_assets import bundled_root

_FILE = "chat_completions_responses_special_instructions.md"


def responses_special_instructions_path() -> str:
    """Абсолютный путь к ролевой вставке режима Responses."""
    return os.path.join(str(bundled_root()), "prompts", _FILE)
