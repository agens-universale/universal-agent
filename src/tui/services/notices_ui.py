"""Общие хелперы счётчика уведомлений для карточки и футера TUI."""
from __future__ import annotations

from typing import List, Tuple

from rich.text import Text

from startup_notices import Notice

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
# Подсказка-хвост бейджа — ЕДИНЫЙ источник: её же снимает сторож двух рядов,
# ужимая бейдж до одних счётчиков.
_BADGE_HINT_SUFFIX = " · F4/notices"


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
    return f"{badge}{_BADGE_HINT_SUFFIX}" if with_hint else badge


def _badge_counts_only(badge: str) -> str:
    """Тот же бейдж без подсказки — одни счётчики (ступень ужатия сторожа).

    Снимать хвост у готовой строки, а не пересобирать бейдж из `Notice`, —
    осознанно: сторож получает уже собранный текст, а формат хвоста здесь же,
    в `format_notices_badge`, и другого источника у него нет."""
    return (badge[:-len(_BADGE_HINT_SUFFIX)]
            if badge.endswith(_BADGE_HINT_SUFFIX) else badge)


def _wrapped_lines(text: str, width: int) -> List[str]:
    """Ряды, которыми `text` ляжет на ширину `width` — той же меркой, какой его
    рисует Rich (перенос по словам), с учётом явных переносов."""
    from rich.console import Console

    console = Console(width=width)
    out: List[str] = []
    for line in text.split("\n"):
        wrapped = Text(line).wrap(console, width)
        if not wrapped:
            out.append("")          # пустая строка — тоже ряд
            continue
        out.extend(seg.plain.rstrip() for seg in wrapped)
    return out


def _wrapped_rows(text: str, width: int) -> int:
    """Сколько РЯДОВ займёт `text` при ширине `width`."""
    return len(_wrapped_lines(text, width))


def _clamp_two_rows(text: str, width: int) -> str:
    """Безусловный потолок в два ряда: третьего ряда у футера (height: 2) нет."""
    lines = _wrapped_lines(text, width)
    return text if len(lines) <= 2 else "\n".join(lines[:2])


def _footer_left_layout(commands: str, badge: str, *, width: int) -> Tuple[str, int]:
    """Сборка левой колонки и число рядов, которое она ЗАЙМЁТ при этой ширине.

    Общее ядро для отрисовки (`footer_left_with_badge`) и для оценки «сколько
    рядов» (`footer_left_rows`): вторая нужна лесенке правой колонки, и считать
    её отдельной меркой нельзя — оценка и отрисовка разъедутся. Рядов здесь
    столько, сколько сборка ХОЧЕТ занять: сторож двух рядов накладывается
    поверх, в отрисовке."""
    if not badge:
        return commands, (1 if width <= 0 else _wrapped_rows(commands, width))
    joined = f"{commands}  ·  {badge}"
    if width <= 0:
        return joined, 1
    from rich.cells import cell_len
    from rich.console import Console

    lines = Text(commands).wrap(Console(width=width), width)
    last = lines[-1].plain if lines else ""
    if cell_len(last) + cell_len(f"  ·  {badge}") <= width:
        return joined, _wrapped_rows(joined, width)
    return f"{commands}\n{badge}", len(lines) + _wrapped_rows(badge, width)


def footer_left_rows(commands: str, badge: str, *, width: int) -> int:
    """Сколько рядов ЗАПРОСИТ левая колонка футера при ширине `width`.

    Считается ДО сторожа: правая колонка выбирает по этому числу, сколько места
    оставить левой (лесенка `TraceScreen._compose_footer_right`), и должна
    видеть честное «не влезло», а не уже урезанный сторожем результат."""
    return _footer_left_layout(commands, badge, width=width)[1]


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
    resize/refresh пересоберёт по фактическому месту.

    Сторож двух рядов (Task 17): футер высотой 2, третий ряд обрезается — и
    обрезался бы именно бейдж, выселенный под уже перенесённые команды. Сторож
    САМОДОСТАТОЧЕН: последняя ступень лесенки правой колонки возвращается
    безусловно, без предиката по рядам левой, и именно она загоняет левую в
    ширины, где сборка сама в два ряда не укладывается. Полагаться на то, что
    правая столько не отберёт, нельзя — потолок ставится здесь и всегда.

    Порядок жертв слева (фикс-раунд 2 по ревью): сначала ужимается БЕЙДЖ —
    подсказка «F4/notices» уходит, счётчики остаются; затем теряет хвост список
    подсказок; последними снимаются счётчики. Команды не выбрасываются целиком
    никогда: во время хода в них живёт `Ctrl+C/stop`, и терять его нельзя ни на
    какой ширине."""
    text, rows = _footer_left_layout(commands, badge, width=width)
    if rows <= 2 or width <= 0:
        return text

    shrunk = badge
    if _wrapped_rows(shrunk, width) > 1:
        shrunk = _badge_counts_only(badge)          # подсказка уходит первой
        if _wrapped_rows(shrunk, width) > 1:
            shrunk = ""                             # не влезли и счётчики
    if shrunk and shrunk != badge:
        text, rows = _footer_left_layout(commands, shrunk, width=width)
        if rows <= 2:
            return text
    badge_rows = 1 if shrunk else 0
    kept = _wrapped_lines(commands, width)[:2 - badge_rows]
    return _clamp_two_rows("\n".join([*kept, shrunk] if shrunk else kept), width)
