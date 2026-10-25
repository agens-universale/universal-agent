"""Знак модели — точки уровней и имя; единственный владелец цветов уровней.

Точки считаются по ЯВНЫМ слотам (`settings.slots`): модель, назначенная
уровню, несёт его точку; без уровня — одна приглушённая; `auto` — одна
фирменная. Слово уровня как выбор показывает модель своего слота.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from gc_core.config_modules.model_registry import LEVELS
from rich.text import Text

from gc_tui.palette import ACCENT, BRAND, DIM, INK, RUN_COLOR

LEVEL_COLORS: Mapping[str, str] = {
    "light": INK,
    "default": RUN_COLOR,
    "max": ACCENT,
}
AUTO_COLOR = BRAND
FREE_COLOR = DIM

_DOT = "●"
_AUTO = "auto"


def levels_of(alias: str, slots: Mapping[str, str | None]) -> tuple[str, ...]:
    """Уровни, явно назначенные модели, в порядке light, default, max."""
    return tuple(lv for lv in LEVELS if slots.get(lv) == alias)


def dots(choice: str, slots: Mapping[str, str | None]) -> Text:
    """Точки выбора: auto, слово уровня или алиас реестра."""
    if choice == _AUTO:
        return Text(_DOT, style=AUTO_COLOR)
    if choice in LEVEL_COLORS:
        return Text(_DOT, style=LEVEL_COLORS[choice])
    levels = levels_of(choice, slots)
    if not levels:
        return Text(_DOT, style=FREE_COLOR)
    text = Text()
    for level in levels:
        text.append(_DOT, style=LEVEL_COLORS[level])
    return text


def sign(
    choice: str,
    slots: Mapping[str, str | None],
    *,
    name_style: str = INK,
    alias: str | None = None,
) -> Text:
    """Точки, пробел, имя. Слово уровня с `alias` — полный знак модели
    этого слота (точки всех её уровней); без `alias` — одна точка уровня."""
    shown = alias if alias is not None else choice
    text = Text.assemble(dots(shown, slots), " ")
    text.append(alias if alias is not None else choice, style=name_style)
    return text


def dots_width(aliases: Iterable[str], slots: Mapping[str, str | None]) -> int:
    """Ширина колонки точек: максимум уровней среди алиасов, не меньше 1."""
    return max([1, *(len(levels_of(a, slots)) for a in aliases)])


def level_hint(alias: str, slots: Mapping[str, str | None]) -> str:
    """Подсказка пикера про уровни модели."""
    levels = levels_of(alias, slots)
    if not levels:
        return "без уровня"
    if len(levels) == len(LEVELS):
        return "все три уровня"
    if len(levels) == 1:
        return f"уровень {levels[0]}"
    return f"уровни {levels[0]} и {levels[1]}"
