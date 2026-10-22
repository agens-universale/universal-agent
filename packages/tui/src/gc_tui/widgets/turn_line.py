"""Осевшая строка итога хода в ленте (P1b).

Одна строка уровня 0: глиф в колонке `●`, пустая строка сверху, тусклый
цвет — как строка итога индикатора, но без сводки правок и причины.
Строит её `trace_render.build_turn_line` — один билдер для ленты и
превью. Строка последнего хода стоит в ленте скрытой (класс `-latest`),
пока её показывает индикатор."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

#: Строка последнего хода: в ленте скрыта, её показывает индикатор.
TURN_LINE_LATEST = "-latest"


class TurnLine(Static):
    """Строка итога одного хода. `opened_by` — реплика, открывшая ход
    (из записи итога): по нему откат реплики снимает строку."""

    def __init__(self, text: Text, *, opened_by: str | None) -> None:
        super().__init__(text, markup=False)
        self.text = text
        self.opened_by = opened_by
