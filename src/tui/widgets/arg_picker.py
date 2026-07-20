"""ArgPicker — overlay-список значений аргумента команды (value + описание).

Зеркало `SkillPicker`/`CommandPalette`: те же CSS-классы (.palette / .palette-row /
.palette-hint) и модель состояния (filter + selected index). Опции приходят готовым
`list[tuple[str, str]]` (value, description) из `Command.arg_options` — открывает
вызывающий код (TraceScreen) для любой команды с непустым arg_options.
"""
from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static


_HINT = "↑↓ выбрать · Tab дополнить · Enter выполнить · Esc закрыть"
_NO_MATCH = "(нет совпадений)"


class ArgPicker(VerticalScroll):
    """Filtered overlay-список значений аргумента."""

    DEFAULT_CSS = ""  # стили — .palette / .palette-row / .palette-hint в styles.tcss

    def __init__(
        self,
        options: list[tuple[str, str]],
        initial_filter: str = "",
    ) -> None:
        super().__init__(classes="palette")
        self._options = options
        self._filter = initial_filter
        self._selected = 0
        self._apply_size()  # честная высота с самого первого layout-прохода (см. _apply_size)

    def compose(self) -> ComposeResult:
        rows = self._visible_rows()
        if not rows:
            yield Static(_NO_MATCH, classes="palette-hint", markup=False)
        else:
            for i, opt in enumerate(rows):
                classes = "palette-row selected" if i == self._selected else "palette-row"
                yield Static(self._format_row(opt), classes=classes, markup=False)
        yield self._build_footer()

    def _build_footer(self) -> Vertical:
        """Докнутый подвал (паттерн CommandPalette._build_footer): hint — ПОСЛЕДНИЙ
        FLOW-ребёнок в старой раскладке упирался в max-height-клэмп длинного списка
        и прятался за скроллом (регрессия воздуха .palette-keys, коммит c0622c9a).
        Докнутый контейнер вне скролла держит hint видимым при открытии всегда."""
        return Vertical(Static(_HINT, classes="palette-hint palette-keys", markup=False),
                         classes="palette-footer")

    def _value_col_w(self) -> int:
        """Ширина value-колонки: value паддируется до самого длинного value
        среди ВИДИМЫХ (отфильтрованных) опций + 3 пробела (то же правило
        выравнивания, что и в CommandPalette, спека 2026-07-18 §4)."""
        rows = self._visible_rows()
        if not rows:
            return 0
        return max(len(v) for v, _ in rows) + 3

    def _format_row(self, opt: tuple[str, str]) -> str:
        value, desc = opt
        if not desc:
            return f" {value}"
        return f" {value.ljust(self._value_col_w())}{desc}"

    def _visible_rows(self) -> list[tuple[str, str]]:
        f = self._filter.strip().lower()
        if not f:
            return list(self._options)
        return [o for o in self._options if o[0].lower().startswith(f)]

    def _apply_size(self) -> None:
        """Textual не учитывает докнутых детей (`.palette-footer`, `dock: bottom`)
        в auto-высоте контейнера (см. подробный докстринг CommandPalette._apply_size) —
        без резервации подвал либо схлопывает контейнер, либо рисуется поверх хвоста
        списка. `min-height` поднимает итоговый размер до честного, не трогая
        declared `height: auto`."""
        self.styles.min_height = self.desired_height

    @property
    def desired_height(self) -> int:
        """Честная желаемая высота: строки списка (или одна no-match-строка) +
        докнутый footer (margin-top hint'а(1) + строка hint'а(1)) + рамка .palette(2).
        main.py::_sync_trace_max_height берёт это значение напрямую — недосчёт
        выдавливал бы нижнюю рамку пикера за экран."""
        rows = self._visible_rows()
        flow = len(rows) if rows else 1
        return flow + 2 + 2

    def set_filter(self, prefix: str) -> None:
        self._filter = prefix
        self._selected = 0
        self._rebuild()

    def move_selection(self, delta: int) -> None:
        rows = self._visible_rows()
        if not rows:
            return
        self._selected = (self._selected + delta) % len(rows)
        self._rebuild()

    def selected_value(self) -> Optional[str]:
        rows = self._visible_rows()
        if not rows:
            return None
        idx = max(0, min(self._selected, len(rows) - 1))
        return rows[idx][0]

    def _rebuild(self) -> None:
        if not self.is_mounted:
            return
        rows = self._visible_rows()
        new_widgets: list = []
        selected_widget: Optional[Static] = None
        if not rows:
            new_widgets.append(Static(_NO_MATCH, classes="palette-hint", markup=False))
        else:
            for i, opt in enumerate(rows):
                classes = "palette-row selected" if i == self._selected else "palette-row"
                w = Static(self._format_row(opt), classes=classes, markup=False)
                if i == self._selected:
                    selected_widget = w
                new_widgets.append(w)
        new_widgets.append(self._build_footer())
        self._apply_size()
        self.remove_children()
        self.mount_all(new_widgets)
        if selected_widget is not None:
            self.call_after_refresh(selected_widget.scroll_visible, animate=False)
