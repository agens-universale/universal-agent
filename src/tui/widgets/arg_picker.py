"""ArgPicker — overlay-список значений аргумента команды (value + описание).

Зеркало `SkillPicker`/`CommandPalette`: те же CSS-классы (.palette / .palette-row /
.palette-hint) и модель состояния (filter + selected index). Опции приходят готовым
`list[tuple[str, str]]` (value, description) из `Command.arg_options` — открывает
вызывающий код (TraceScreen) для любой команды с непустым arg_options.
"""
from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.containers import VerticalScroll
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

    def compose(self) -> ComposeResult:
        rows = self._visible_rows()
        if not rows:
            yield Static(_NO_MATCH, classes="palette-hint", markup=False)
        else:
            for i, opt in enumerate(rows):
                classes = "palette-row selected" if i == self._selected else "palette-row"
                yield Static(self._format_row(opt), classes=classes, markup=False)
        yield Static(_HINT, classes="palette-hint palette-keys", markup=False)

    def _format_row(self, opt: tuple[str, str]) -> str:
        value, desc = opt
        return f" {value}   {desc}" if desc else f" {value}"

    def _visible_rows(self) -> list[tuple[str, str]]:
        f = self._filter.strip().lower()
        if not f:
            return list(self._options)
        return [o for o in self._options if o[0].lower().startswith(f)]

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
        new_widgets: list[Static] = []
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
        new_widgets.append(Static(_HINT, classes="palette-hint palette-keys", markup=False))
        self.remove_children()
        self.mount_all(new_widgets)
        if selected_widget is not None:
            self.call_after_refresh(selected_widget.scroll_visible, animate=False)
