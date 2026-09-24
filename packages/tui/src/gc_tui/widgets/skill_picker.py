"""SkillPicker — overlay-список скиллов с substring-фильтром по id (§ design 2026-05-22).

Зеркало `CommandPalette`: те же CSS-классы (.palette / .palette-row / .palette-hint),
та же модель состояния (filter + selected index). Принимает готовый `list[dict]` —
загрузку из реестра делает вызывающий код (TraceScreen).
"""

from __future__ import annotations

from typing import Optional

from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from gc_tui.widgets.palette_rows import PaletteRowsMixin

_HINT = "↑↓ выбрать · Tab дополнить · Enter выполнить · Esc закрыть"


class SkillPicker(PaletteRowsMixin, VerticalScroll):
    """Filtered overlay-список скиллов.

    Навигация (`set_filter` / `move_selection`), `compose` и `_rebuild` — общие
    с `ArgPicker`, в `PaletteRowsMixin`."""

    DEFAULT_CSS = (
        ""  # стили — .palette / .palette-row / .palette-hint в styles.tcss
    )

    def __init__(
        self,
        skills: list[dict],
        initial_filter: str = "",
    ) -> None:
        super().__init__(classes="palette")
        self._skills = skills
        self._filter = initial_filter
        self._selected = 0
        self._apply_size()  # честная высота с самого первого layout-прохода (см. _apply_size)

    def _build_footer(self) -> Vertical:
        """Докнутый подвал (паттерн CommandPalette._build_footer): hint — ПОСЛЕДНИЙ
        FLOW-ребёнок в старой раскладке упирался в max-height-клэмп длинного списка
        и прятался за скроллом (регрессия воздуха .palette-keys, коммит c0622c9a).
        Докнутый контейнер вне скролла держит hint видимым при открытии всегда."""
        return Vertical(
            Static(_HINT, classes="palette-hint palette-keys", markup=False),
            classes="palette-footer",
        )

    def _format_row(self, skill: dict) -> str:
        return f" {skill.get('id', '?')}"

    def _visible_rows(self) -> list[dict]:
        f = self._filter.lower()
        if not f:
            return list(self._skills)
        return [s for s in self._skills if f in str(s.get("id", "")).lower()]

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

    def selected_skill(self) -> Optional[dict]:
        return self._selected_row()
