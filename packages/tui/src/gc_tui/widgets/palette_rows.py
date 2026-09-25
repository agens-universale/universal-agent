"""Общая модель состояния overlay-списков `ArgPicker` и `SkillPicker`: filter + selected index.

Оба пикера — зеркала `CommandPalette` с теми же CSS-классами (.palette / .palette-row /
.palette-hint) и держали по копии одной и той же навигации: сброс подсветки при смене
фильтра, заворот стрелок по кругу, зажим индекса и перерисовку строк с докнутым
подвалом. Копии здесь сведены в одну; различаются пикеры только тем, ЧТО показывают
(`_visible_rows` / `_format_row` / `_build_footer` / `_apply_size`), — это остаётся у
каждого свой.
"""

from __future__ import annotations

from typing import Any, Optional

from textual.app import ComposeResult
from textual.widgets import Static

_NO_MATCH = "(нет совпадений)"


class PaletteRowsMixin:
    """Навигация и перерисовка строк overlay-списка поверх `VerticalScroll`.

    Ставится ПЕРЕД виджетом в базах: `class X(PaletteRowsMixin, VerticalScroll)`.
    Textual-методы (`is_mounted`, `remove_children`, `mount_all`,
    `call_after_refresh`) приходят от виджета; от наследника ждутся поля
    `_filter`/`_selected` и методы `_visible_rows()`, `_format_row(row)`,
    `_build_footer()`, `_apply_size()`.

    Миксин не наследует `DOMNode`, поэтому textual собирает CSS-имена типов и
    bindings по-прежнему только с виджетных баз — селекторы `ArgPicker` /
    `SkillPicker` в стилях от него не меняются."""

    _filter: str
    _selected: int

    def compose(self) -> ComposeResult:
        rows, _ = self._row_widgets()
        yield from rows
        yield self._build_footer()

    def _row_widgets(self) -> tuple[list[Static], Optional[Static]]:
        """Строки видимых опций (или одна no-match-строка) и виджет подсвеченной
        строки — его `_rebuild` докручивает в видимую область."""
        rows = self._visible_rows()
        if not rows:
            return [
                Static(_NO_MATCH, classes="palette-hint", markup=False)
            ], None
        widgets: list[Static] = []
        selected_widget: Optional[Static] = None
        for i, row in enumerate(rows):
            classes = (
                "palette-row selected"
                if i == self._selected
                else "palette-row"
            )
            w = Static(self._format_row(row), classes=classes, markup=False)
            if i == self._selected:
                selected_widget = w
            widgets.append(w)
        return widgets, selected_widget

    def set_filter(self, text: str) -> None:
        self._filter = text
        self._selected = 0
        self._rebuild()

    def move_selection(self, delta: int) -> None:
        rows = self._visible_rows()
        if not rows:
            return
        self._selected = (self._selected + delta) % len(rows)
        self._rebuild()

    def _selected_row(self) -> Any:
        """Подсвеченная строка (индекс зажат в границы видимых) или None, если их нет."""
        rows = self._visible_rows()
        if not rows:
            return None
        idx = max(0, min(self._selected, len(rows) - 1))
        return rows[idx]

    def _rebuild(self) -> None:
        if not self.is_mounted:
            return
        rows, selected_widget = self._row_widgets()
        new_widgets: list = [*rows, self._build_footer()]
        self._apply_size()
        self.remove_children()
        self.mount_all(new_widgets)
        if selected_widget is not None:
            self.call_after_refresh(
                selected_widget.scroll_visible, animate=False
            )
