"""Палитра команд — overlay над вводом (§2.7 спеки)."""
from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from tui.commands import Command, CommandRegistry

_HINT_NO_ARGS = "↑↓ выбрать · Enter выполнить · Esc закрыть"
_HINT_WITH_ARGS = "↑↓ выбрать · Enter ввести аргумент · Esc закрыть"


def _hint_for(cmd: Command | None) -> str:
    """Динамический хинт: есть схема args → Enter подставит команду в поле."""
    if cmd is not None and cmd.args:
        return _HINT_WITH_ARGS
    return _HINT_NO_ARGS


class CommandPalette(VerticalScroll):
    """Filtered overlay-список команд из реестра (§2.7)."""

    DEFAULT_CSS = ""  # стили в styles.tcss (.palette / .palette-row / .palette-hint)

    def __init__(
        self,
        registry: CommandRegistry,
        initial_filter: str = "",
    ) -> None:
        super().__init__(classes="palette")
        self._registry = registry
        self._filter = initial_filter
        self._selected = 0

    def compose(self) -> ComposeResult:
        for i, cmd in enumerate(self._visible_rows()):
            classes = "palette-row selected" if i == self._selected else "palette-row"
            yield Static(self._format_row(cmd), classes=classes, markup=False)
        yield Static(_hint_for(self.selected_command()), classes="palette-hint palette-keys", markup=False)

    def _format_row(self, cmd: Command) -> str:
        return f" /{cmd.name}   {cmd.description}"

    def _visible_rows(self) -> list[Command]:
        f = self._filter.lower()
        all_cmds = self._registry.list_commands()
        if not f:
            return all_cmds
        return [c for c in all_cmds if c.name.startswith(f)]

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

    def selected_command(self) -> Optional[Command]:
        rows = self._visible_rows()
        if not rows:
            return None
        idx = max(0, min(self._selected, len(rows) - 1))
        return rows[idx]

    def _rebuild(self) -> None:
        if not self.is_mounted:
            return
        rows = self._visible_rows()
        new_widgets: list[Static] = []
        selected_widget: Optional[Static] = None
        for i, cmd in enumerate(rows):
            classes = "palette-row selected" if i == self._selected else "palette-row"
            w = Static(self._format_row(cmd), classes=classes, markup=False)
            if i == self._selected:
                selected_widget = w
            new_widgets.append(w)
        new_widgets.append(Static(_hint_for(self.selected_command()), classes="palette-hint palette-keys", markup=False))
        self.remove_children()
        self.mount_all(new_widgets)
        if selected_widget is not None:
            self.call_after_refresh(selected_widget.scroll_visible, animate=False)
