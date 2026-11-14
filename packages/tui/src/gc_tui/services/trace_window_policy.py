"""Политика окна ленты: какие ходы смонтированы (спека шага 2 окна ленты,
§5.3, §6).

Чистые функции над числами виджетов по ходам, без Textual. Единица —
виджет поддерева ленты (то, что раскладывает компоновщик); ход —
минимальная единица окна: гигантский ход идёт целиком, пока он в окне,
лимит превышен. Числа стартовые, подбираются замером (§6). Инвариант
``WINDOW_LIMIT >= TAIL_MIN + 2 * CHUNK_MIN``: иначе домонтирование и
снятие зациклятся.
"""

from __future__ import annotations

from collections.abc import Sequence

#: Лимит окна: столько виджетов поддерева ленты держится у низа.
WINDOW_LIMIT = 1500
#: Хвост при открытии — не меньше стольких виджетов.
TAIL_MIN = 600
#: Кусок истории, домонтируемый сверху, — не меньше стольких виджетов.
CHUNK_MIN = 300
#: Запас до верха смонтированного, в экранах: меньше — следующий кусок.
PREFETCH_SCREENS = 2
#: Виджетов поддерева на элемент модели — оценка ещё не смонтированного
#: хода: до монтажа его поддерево неизвестно (дети собираются при
#: монтаже). Смонтированные ходы считаются по ленте.
ITEM_WIDGETS = 6


def _start_back(end: int, widget_counts: Sequence[int], budget: int) -> int:
    """Начало целых ходов перед ``end``, набравших ``budget`` виджетов."""
    start, total = end, 0
    while start > 0 and total < budget:
        start -= 1
        total += widget_counts[start]
    return start


def tail_turns(widget_counts: Sequence[int], budget: int) -> range:
    """Последние ходы, набравшие ``budget`` виджетов (или все)."""
    end = len(widget_counts)
    return range(_start_back(end, widget_counts, budget), end)


def prev_chunk(first: int, widget_counts: Sequence[int], chunk: int) -> range:
    """Ходы перед ``first``, набравшие ``chunk`` виджетов (или все)."""
    return range(_start_back(first, widget_counts, chunk), first)


def trim_to(mounted: int, limit: int, chunk: int) -> int | None:
    """Сколько виджетов снять, чтобы окно стало не больше ``limit``;
    ``None`` — не снимать: снятие только при ``mounted > limit + chunk``
    (запас против петли домонтирование ↔ снятие)."""
    if mounted > limit + chunk:
        return mounted - limit
    return None


def drop_count(widget_counts: Sequence[int], excess: int) -> int:
    """Сколько первых ходов снять, чтобы ушло не меньше ``excess``
    виджетов; последний ход остаётся."""
    dropped = total = 0
    while dropped < len(widget_counts) - 1 and total < excess:
        total += widget_counts[dropped]
        dropped += 1
    return dropped


def near_top(scroll_y: float, height: int) -> bool:
    """До верха смонтированного меньше запаса ``PREFETCH_SCREENS``."""
    return scroll_y < PREFETCH_SCREENS * height


def underfilled(max_scroll_y: float, height: int) -> bool:
    """Лента ниже запаса ``PREFETCH_SCREENS``: смонтированного не хватает,
    чтобы прокрутить к истории выше."""
    return max_scroll_y < PREFETCH_SCREENS * height
