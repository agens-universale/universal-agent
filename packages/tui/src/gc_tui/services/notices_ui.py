"""Общие хелперы счётчика уведомлений для карточки и футера TUI."""
from __future__ import annotations

from typing import List, Tuple

from rich.text import Text

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


def notice_counts(notices: List[Notice]) -> Tuple[int, int]:
    warns = sum(1 for n in notices if n.level == "warning")
    infos = sum(1 for n in notices if n.level == "info")
    return warns, infos


def format_notices_badge(notices: List[Notice], *, with_hint: bool = True) -> str:
    """Строка вида «⚠ 2  ℹ 1 · F4/notices»; пустая, если уведомлений нет.

    Внутри пары — пробел (см. WARN_GLYPH: он и есть защита от разной ширины значка),
    между парами — двойной. С одиночным пробелом всюду «⚠ 2 ℹ 1» читается как четыре
    отдельных знака: пара держится только тем, что её просвет уже соседского.

    Точка ставится лишь перед подсказкой, отделяя счёт от действия: сами счётчики —
    одна величина и разделять их точкой незачем (смок владельца; синк 2026-08-06
    подтвердил решение против варианта develop).

    Пару «значок+число» от разрыва переносом бережёт не склейка, а сборщик
    `footer_left_with_badge`: он двигает блок целиком. Проверено замером на ширинах
    30–120 — не рвётся ни на одной."""
    warns, infos = notice_counts(notices)
    if not warns and not infos:
        return ""
    counts = [f"{WARN_GLYPH} {warns}"] if warns else []
    if infos:
        counts.append(f"{INFO_GLYPH} {infos}")
    badge = "  ".join(counts)
    return f"{badge} · F4/notices" if with_hint else badge


def footer_left_with_badge(commands: str, badge: str, *, width: int) -> str:
    """Команды footer-left + бейдж уведомлений, собранные ПО МЕСТУ.

    Точка перед бейджем — разделитель соседей по строке, а не часть блока: она
    ставится, только когда блок встаёт в одну строку с командами. Не влезает —
    блок уходит вниз ЦЕЛИКОМ (явным переносом) и без ведущей точки: в начале
    строки разделителю нечего разделять. Отдать это Rich-переносу нельзя — он
    режет блок по внутренним пробелам («ℹ1 ·» наверху, «F4/notices» внизу
    читались как два обрывка, живая приёмка), а неразрывный пробел от него не
    спасает (режет по любому Unicode-пробелу, замерено).

    Решение принимает ПОСЛЕДНЯЯ строка команд после переноса той же меркой, какой
    рисует Rich (Text.wrap): команды и сами переносятся на узком окне. width <= 0
    (layout ещё не прошёл, первый кадр) — оптимистичная сборка с точкой: ближайший
    resize/refresh пересоберёт по фактическому месту."""
    if not badge:
        return commands
    joined = f"{commands}  ·  {badge}"
    if width <= 0:
        return joined
    from rich.cells import cell_len
    from rich.console import Console

    lines = Text(commands).wrap(Console(width=width), width)
    last = lines[-1].plain if lines else ""
    if cell_len(last) + cell_len(f"  ·  {badge}") <= width:
        return joined
    return f"{commands}\n{badge}"
