"""Поиск по прокрутке ленты — чистые функции без геометрии экрана
(спека P3 §2.6, П5)."""

from __future__ import annotations

from bisect import bisect_left
from typing import TYPE_CHECKING, Callable, Protocol, Sequence

if TYPE_CHECKING:
    from textual.geometry import Region
    from textual.widget import Widget


class Placement(Protocol):
    """Ребёнок ленты в координатах её содержимого."""

    region: Region
    widget: Widget


def reply_above(
    placements: Sequence[Placement],
    scroll_y: int,
    is_reply: Callable[[Widget], bool],
) -> Widget | None:
    """Ближайшая реплика, чья верхняя строка выше верхнего края видимой
    области (`region.y < scroll_y`). `placements` — дети ленты в её
    порядке, верх по возрастанию; реплику отличает `is_reply`."""
    index = bisect_left(placements, scroll_y, key=lambda p: p.region.y)
    for placement in reversed(placements[:index]):
        if is_reply(placement.widget):
            return placement.widget
    return None
