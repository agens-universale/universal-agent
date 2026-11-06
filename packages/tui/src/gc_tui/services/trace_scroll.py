"""Поиск по прокрутке ленты без Textual (спека P3 §2.6)."""

from __future__ import annotations

from bisect import bisect_left
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")


def reply_above(
    replies: Sequence[T], scroll_y: int, top: Callable[[T], int]
) -> T | None:
    """Ближайшая реплика, чья верхняя строка выше верхнего края видимой
    области (`top(reply) < scroll_y`). `replies` — в порядке ленты, верх
    по возрастанию; `top` читается в момент поиска и только у реплик,
    которые смотрит бисекция."""
    index = bisect_left(replies, scroll_y, key=top)
    return replies[index - 1] if index else None
