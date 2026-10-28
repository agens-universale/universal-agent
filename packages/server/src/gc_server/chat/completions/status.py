"""Исход ядра → finish_reason чанка Chat Completions."""

from __future__ import annotations

_LENGTH = frozenset({"token_limit", "step_limit", "tool_call_limit"})


def finish_reason(outcome: str | None) -> str:
    """Лимит хода — ``length``. Обычный возврат и ``tool_errors`` — ``stop``."""
    if outcome in _LENGTH:
        return "length"
    return "stop"
