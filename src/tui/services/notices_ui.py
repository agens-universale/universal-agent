"""Общие хелперы счётчика уведомлений для карточки и футера TUI."""
from __future__ import annotations

from typing import List, Tuple

from rich.text import Text

from startup_notices import Notice

# Значки уровней — ЕДИНЫЙ источник для бейджа (футер/карточка//sessions) и экрана
# уведомлений. Вариационный селектор U+FE0F обязателен: терминал рисует эти символы в
# ДВЕ ячейки, а без селектора Rich считает их за одну — и склеенное с ними число
# садилось прямо на вторую половину значка (живая приёмка). С селектором расчётная
# ширина совпадает с нарисованной, число встаёт следом, и отдельный пробел между ними
# не нужен — а значит, переносу строки нечего разрывать.
WARN_GLYPH = "⚠️"
INFO_GLYPH = "ℹ️"


def notice_counts(notices: List[Notice]) -> Tuple[int, int]:
    warns = sum(1 for n in notices if n.level == "warning")
    infos = sum(1 for n in notices if n.level == "info")
    return warns, infos


def format_notices_badge(notices: List[Notice], *, with_hint: bool = True) -> str:
    """Строка вида «⚠2 ℹ1 · F4/notices»; пустая, если уведомлений нет.

    Счётчики — одна величина, разделять их точкой незачем: они читаются парой, и
    точка ставится только перед подсказкой, отделяя счёт от действия. Между самими
    счётчиками хватает одного пробела — второй здесь зрительно лишний (смок
    владельца; синк 2026-08-06 подтвердил решение против варианта develop).

    Глиф и его число — БЕЗ пробела между собой: перенос строки в футере рвал пару, и
    «ℹ» оставалась наверху, а «2» уезжала вниз (живая приёмка). Неразрывный пробел не
    спасает — перенос Rich режет по любому Unicode-пробелу, проверено замером; склейка
    единственная надёжная. Просвет даёт сам значок: с вариационным селектором он ровно
    двухклеточный (см. WARN_GLYPH)."""
    warns, infos = notice_counts(notices)
    if not warns and not infos:
        return ""
    counts = [f"{WARN_GLYPH}{warns}"] if warns else []
    if infos:
        counts.append(f"{INFO_GLYPH}{infos}")
    badge = " ".join(counts)
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
