"""FilePicker — overlay-список путей для @-аттача. Зеркало SkillPicker.

Отличие: фильтрацию/листинг делает экран (через file_index), сюда приходят
готовые строки путей. Те же CSS-классы (.palette / .palette-row / .palette-hint)."""
from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

_HINT = "↑↓ выбрать · Tab/Enter дополнить · Esc закрыть"
_NO_MATCH = "(нет совпадений)"


class FilePicker(VerticalScroll):
    """Filtered overlay-список путей (строки; папки с хвостовым '/')."""

    DEFAULT_CSS = ""  # стили — .palette / .palette-row / .palette-hint в styles.tcss

    def __init__(self, entries: list[str]) -> None:
        super().__init__(classes="palette")
        self._entries = list(entries)
        self._selected = 0

    def compose(self) -> ComposeResult:
        if not self._entries:
            yield Static(_NO_MATCH, classes="palette-hint", markup=False)
        else:
            for i, entry in enumerate(self._entries):
                classes = "palette-row selected" if i == self._selected else "palette-row"
                yield Static(f" {entry}", classes=classes, markup=False)
        yield Static(_HINT, classes="palette-hint", markup=False)

    def set_entries(self, entries: list[str]) -> None:
        self._entries = list(entries)
        self._selected = 0
        self._rebuild()

    def move_selection(self, delta: int) -> None:
        if not self._entries:
            return
        self._selected = (self._selected + delta) % len(self._entries)
        self._rebuild()

    def selected_entry(self) -> Optional[str]:
        if not self._entries:
            return None
        idx = max(0, min(self._selected, len(self._entries) - 1))
        return self._entries[idx]

    def _rebuild(self) -> None:
        if not self.is_mounted:
            return
        new_widgets: list[Static] = []
        selected_widget: Optional[Static] = None
        if not self._entries:
            new_widgets.append(Static(_NO_MATCH, classes="palette-hint", markup=False))
        else:
            for i, entry in enumerate(self._entries):
                classes = "palette-row selected" if i == self._selected else "palette-row"
                w = Static(f" {entry}", classes=classes, markup=False)
                if i == self._selected:
                    selected_widget = w
                new_widgets.append(w)
        new_widgets.append(Static(_HINT, classes="palette-hint", markup=False))
        self.remove_children()
        self.mount_all(new_widgets)
        if selected_widget is not None:
            self.call_after_refresh(selected_widget.scroll_visible, animate=False)
