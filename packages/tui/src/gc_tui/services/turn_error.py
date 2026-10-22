"""Причина ошибки хода для строки `└` под индикатором `✕ Ошибка`.

Коротко: класс исключения и первая строка его сообщения; длинное —
с `…`. Текст хранится в meta треда (`tui.last_error`)."""

from __future__ import annotations

#: Предел причины в символах (как у текста ошибки в строке вызова).
TURN_ERROR_LIMIT = 160


def describe_turn_error(exc: BaseException) -> str:
    """`Класс: первая строка сообщения`; сообщения нет — только класс."""
    name = exc.__class__.__name__
    lines = str(exc).strip().splitlines()
    reason = f"{name}: {lines[0].strip()}" if lines else name
    if len(reason) <= TURN_ERROR_LIMIT:
        return reason
    return reason[: TURN_ERROR_LIMIT - 1].rstrip() + "…"
