"""Исход ядра → status объекта Response."""

from __future__ import annotations

from gc_core.execution.constants import NO_TOOL_RESPONSE

_INCOMPLETE = frozenset(
    {"token_limit", "step_limit", "tool_call_limit", NO_TOOL_RESPONSE}
)


def status_from_outcome(outcome: str | None) -> str:
    """Обычный возврат, ``tool_errors`` и любой другой исход вне лимитов."""
    if outcome in _INCOMPLETE:
        return "incomplete"
    return "completed"


def turn_status(*, outcome: str | None, cancelled: bool, failed: bool) -> str:
    """Отмена важнее лимита и исключения: статус уже не переписывают позже."""
    if cancelled:
        return "cancelled"
    if failed:
        return "failed"
    return status_from_outcome(outcome)


def incomplete_details(status: str, outcome: str | None) -> dict | None:
    if status != "incomplete":
        return None
    return {"reason": outcome}
