"""Общие хелперы счётчика уведомлений для карточки и футера TUI."""

from __future__ import annotations

from typing import List, Tuple

from gc_core.startup_notices import Notice

# Значки уровней — ЕДИНЫЙ источник для бейджа (футер/карточка//sessions) и экрана
# уведомлений. БЕЗ вариационного селектора и всегда с пробелом перед числом.
#
# Ставки на ширину значка тут быть не может: Windows рисует голый «⚠» в две ячейки,
# macOS — в одну. Селектор U+FE0F (просьба «рисуй цветной картинкой») мы пробовали:
# Rich считает такую пару за 2, на Windows сошлось — а macOS нарисовал картинку с
# наползанием на соседнюю клетку, и приклеенное вплотную число ушло под неё целиком
# (отчёт коллеги 2026-08-18). U+FE0E не спасает: терминал вправе его проигнорировать.
#
# Работает не «правильный значок», а ПРОБЕЛ после него: нарисуется шире — съест
# пробел, и ничего не спрячется. Так живут остальные 45 значков проекта («⚠ MCP: 0/1
# …», «⚠ не удалось проверить…»), и они целы на обеих системах.
WARN_GLYPH = "⚠"
INFO_GLYPH = "ℹ"
# Подсказка-хвост бейджа.
_BADGE_HINT_SUFFIX = " · F4/notices"


def notice_counts(notices: List[Notice]) -> Tuple[int, int]:
    warns = sum(1 for n in notices if n.level == "warning")
    infos = sum(1 for n in notices if n.level == "info")
    return warns, infos


def format_notices_badge(
    notices: List[Notice], *, with_hint: bool = True
) -> str:
    """Строка вида «⚠ 2  ℹ 1 · F4/notices»; пустая, если уведомлений нет.

    Внутри пары — пробел (см. WARN_GLYPH: он и есть защита от разной ширины значка),
    между парами — двойной. С одиночным пробелом всюду «⚠ 2 ℹ 1» читается как четыре
    отдельных знака: пара держится только тем, что её просвет уже соседского.

    Точка ставится лишь перед подсказкой, отделяя счёт от действия: сами счётчики —
    одна величина и разделять их точкой незачем (смок владельца; синк 2026-08-06
    подтвердил решение против варианта develop)."""
    warns, infos = notice_counts(notices)
    if not warns and not infos:
        return ""
    counts = [f"{WARN_GLYPH} {warns}"] if warns else []
    if infos:
        counts.append(f"{INFO_GLYPH} {infos}")
    badge = "  ".join(counts)
    return f"{badge}{_BADGE_HINT_SUFFIX}" if with_hint else badge
