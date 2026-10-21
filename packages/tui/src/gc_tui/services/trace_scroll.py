"""Поиск по прокрутке ленты без Textual (спека P3 §2.6)."""

from __future__ import annotations

from bisect import bisect_left
from typing import Sequence, TypeVar

T = TypeVar("T")


def reply_above(tops: Sequence[tuple[int, T]], scroll_y: int) -> T | None:
    """Ближайшая реплика, чья верхняя строка выше верхнего края видимой
    области (`y < scroll_y`). `tops` — пары `(virtual_region.y, реплика)`
    в порядке ленты, `y` по возрастанию."""
    index = bisect_left(tops, scroll_y, key=lambda top: top[0])
    return tops[index - 1][1] if index else None
