"""Политика окна ленты: какие ходы смонтированы (спека шага 2 окна ленты,
§5.3, §5.11, §6).

Чистые функции над числами виджетов по ходам, без Textual. Единица —
виджет поддерева ленты (то, что раскладывает компоновщик); ход —
минимальная единица окна: гигантский ход идёт целиком, пока он в окне,
лимит превышен. Числа подобраны замером на стенде (§6, §12). Инвариант
``WINDOW_LIMIT >= TAIL_MIN + 2 * CHUNK_MIN``: иначе домонтирование и
снятие зациклятся.
"""

from __future__ import annotations

from collections.abc import Sequence

#: Лимит окна: столько виджетов поддерева ленты держится у низа.
WINDOW_LIMIT = 1000
#: Хвост при открытии — не меньше стольких виджетов.
TAIL_MIN = 200
#: Кусок истории, домонтируемый к краю, — не меньше стольких виджетов.
CHUNK_MIN = 90
#: Запас до края смонтированного, в экранах: меньше — следующий кусок.
PREFETCH_SCREENS = 2
#: Виджетов поддерева на элемент модели — оценка ещё не смонтированного
#: хода: до монтажа его поддерево неизвестно (дети собираются при
#: монтаже). Смонтированные ходы считаются по ленте.
ITEM_WIDGETS = 9


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


def next_chunk(
    start: int, stop: int, widget_counts: Sequence[int], chunk: int
) -> range:
    """Ходы с ``start``, набравшие ``chunk`` виджетов, — не дальше
    ``stop`` (конец разрыва)."""
    end, total = start, 0
    while end < stop and total < chunk:
        total += widget_counts[end]
        end += 1
    return range(start, end)


def edge_drops(widget_counts: Sequence[int], excess: int) -> int:
    """Сколько ходов снять с края, чтобы ушло не меньше ``excess``
    виджетов; ``widget_counts`` — снимаемые ходы от края внутрь (до
    удерживаемых и закреплённых, §5.11)."""
    dropped = total = 0
    while dropped < len(widget_counts) and total < excess:
        total += widget_counts[dropped]
        dropped += 1
    return dropped


def keep_rows(
    scroll_y: float, height: int, *, stuck: bool
) -> tuple[float, float]:
    """Строки ленты, чьи ходы снятие не трогает (§5.11): видимое и, пока
    лента не у низа, два запаса домонтирования с обеих сторон. Кусок
    встаёт на одном запасе — разница в запас и есть гистерезис против
    колебаний колеса. У низа домонтирования нет — только видимое."""
    margin = 0 if stuck else 2 * PREFETCH_SCREENS * height
    return scroll_y - margin, scroll_y + height + margin


def short_gap(gap_widgets: int) -> bool:
    """Разрыв не больше хвоста при открытии: переход в конец его
    домонтирует, а не перемонтирует хвост."""
    return gap_widgets <= TAIL_MIN


def near_edge(distance: float, height: int) -> bool:
    """До края смонтированного (верха или разрыва) меньше запаса
    ``PREFETCH_SCREENS``."""
    return distance < PREFETCH_SCREENS * height


def underfilled(max_scroll_y: float, height: int) -> bool:
    """Лента ниже запаса ``PREFETCH_SCREENS``: смонтированного не хватает,
    чтобы прокрутить к истории выше."""
    return max_scroll_y < PREFETCH_SCREENS * height
