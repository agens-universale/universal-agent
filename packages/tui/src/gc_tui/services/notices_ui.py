"""Общие хелперы счётчика уведомлений для карточки и футера TUI."""

from __future__ import annotations

from typing import List, Tuple

from gc_core.startup_notices import Notice
from rich.text import Text

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


def _wrapped_rows(text: str, width: int) -> int:
    """Сколько РЯДОВ займёт `text` при ширине `width` — той же меркой, какой
    его рисует Rich (перенос по словам), с учётом явных переносов."""
    from rich.console import Console

    console = Console(width=width)
    return sum(
        max(1, len(Text(line).wrap(console, width)))
        for line in text.split("\n")
    )


def footer_left_rows(commands: str, badge: str, *, width: int) -> int:
    """Сколько рядов ЗАПРОСИЛИ бы команды и бейдж при ширине `width`.

    Мерка лесенки правой колонки (`TraceScreen._compose_footer_right`):
    бейдж встаёт в строку команд через точку, если влезает, иначе уходит
    вниз целиком."""
    if not badge:
        return 1 if width <= 0 else _wrapped_rows(commands, width)
    if width <= 0:
        return 1
    from rich.cells import cell_len
    from rich.console import Console

    lines = Text(commands).wrap(Console(width=width), width)
    last = lines[-1].plain if lines else ""
    if cell_len(last) + cell_len(f"  ·  {badge}") <= width:
        return _wrapped_rows(f"{commands}  ·  {badge}", width)
    return len(lines) + _wrapped_rows(badge, width)
