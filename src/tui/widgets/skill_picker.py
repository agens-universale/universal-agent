"""SkillPicker — overlay-список скиллов с substring-фильтром по id (§ design 2026-05-22).

Зеркало `CommandPalette`: те же CSS-классы (.palette / .palette-row / .palette-hint),
та же модель состояния (filter + selected index). Принимает готовый `list[dict]` —
загрузку из реестра делает вызывающий код (TraceScreen).
"""
from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


_HINT = "↑↓ выбрать · Tab дополнить · Enter выполнить · Esc закрыть"
_NO_MATCH = "(нет совпадений)"


class SkillPicker(VerticalScroll):
    """Filtered overlay-список скиллов."""

    DEFAULT_CSS = ""  # стили — .palette / .palette-row / .palette-hint в styles.tcss

    def __init__(
        self,
        skills: list[dict],
        initial_filter: str = "",
    ) -> None:
        super().__init__(classes="palette")
        self._skills = skills
        self._filter = initial_filter
        self._selected = 0

    def compose(self) -> ComposeResult:
        rows = self._visible_rows()
        if not rows:
            yield Static(_NO_MATCH, classes="palette-hint", markup=False)
        else:
            for i, skill in enumerate(rows):
                classes = "palette-row selected" if i == self._selected else "palette-row"
                yield Static(self._format_row(skill), classes=classes, markup=False)
        yield Static(_HINT, classes="palette-hint", markup=False)

    def _format_row(self, skill: dict) -> str:
        return f" {skill.get('id', '?')}"

    def _visible_rows(self) -> list[dict]:
        f = self._filter.lower()
        if not f:
            return list(self._skills)
        return [s for s in self._skills if f in str(s.get("id", "")).lower()]

    def set_filter(self, query: str) -> None:
        self._filter = query
        self._selected = 0
        self._rebuild()

    def move_selection(self, delta: int) -> None:
        rows = self._visible_rows()
        if not rows:
            return
        self._selected = (self._selected + delta) % len(rows)
        self._rebuild()

    def selected_skill(self) -> Optional[dict]:
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
        if not rows:
            new_widgets.append(Static(_NO_MATCH, classes="palette-hint", markup=False))
        else:
            for i, skill in enumerate(rows):
                classes = "palette-row selected" if i == self._selected else "palette-row"
                w = Static(self._format_row(skill), classes=classes, markup=False)
                if i == self._selected:
                    selected_widget = w
                new_widgets.append(w)
        new_widgets.append(Static(_HINT, classes="palette-hint", markup=False))
        self.remove_children()
        self.mount_all(new_widgets)
        if selected_widget is not None:
            self.call_after_refresh(selected_widget.scroll_visible, animate=False)
