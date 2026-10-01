"""Исход хода ядра → stopReason ACP."""

from __future__ import annotations

_MAX_TURN = frozenset({"step_limit", "tool_call_limit"})


def stop_reason(outcome: str | None, *, cancelled: bool) -> str:
    """Отмена важнее лимита: результат промпта уже не переписывают позже."""
    if cancelled:
        return "cancelled"
    if outcome == "token_limit":
        return "max_tokens"
    if outcome in _MAX_TURN:
        return "max_turn_requests"
    return "end_turn"
