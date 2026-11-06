"""SkillPicker — overlay-список скиллов с substring-фильтром по id (§ design 2026-05-22).

Зеркало `CommandPalette`: те же CSS-классы (.palette / .palette-row / .palette-hint),
та же модель состояния (filter + selected index). Принимает готовый `list[dict]` —
загрузку из реестра делает вызывающий код (TraceScreen).

Скиллы без пакета идут первыми, без шапки; затем группы по пакету (часть
id до `:`): светло-серая невыбираемая шапка, под ней с отступом короткие
имена цветом скилла. Имя скилла без пакета стоит на месте шапки, описания
всех строк — серым в одной колонке (геометрия — `palette_layout`, как у
палитры). Порядок — по алфавиту (`_grouped`). Выбор и вставка — по
скиллам, шапки стрелки проходят мимо; вставляется по-прежнему полный id. Шапка над выбранным первым скиллом
группы докручивается в видимую область вместе с ним.
"""

from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from gc_tui import palette as _pal
from gc_tui.widgets.palette_layout import (
    fit_name,
    fit_text,
    fold,
    name_col_w,
    overlay_content_width,
    skill_name_key,
    skill_short,
)
from gc_tui.widgets.palette_rows import PaletteRowsMixin

_HINT = "↑↓ выбрать · Tab дополнить · Enter выполнить · Esc закрыть"
_HEAD = " "  # ведущий пробел строки: здесь шапка и скилл без пакета
_INDENT = "   "  # ведущий пробел + отступ скилла под шапкой (2)


def _pack_of(skill: dict) -> str:
    """Пакет скилла — часть id до `:`; у плоского — пусто."""
    return str(skill.get("id", "")).rpartition(":")[0]


def _grouped(skills: list[dict]) -> list[tuple[str, list[dict]]]:
    """Группы (пакет, скиллы) — единственное место порядка пикера: без
    пакета (пакет "") — первой, затем пакеты по алфавиту; скиллы внутри
    группы — по короткому имени (`skill_name_key`), всё без регистра."""
    groups: dict[str, list[dict]] = {}
    for skill in sorted(skills, key=skill_name_key):
        groups.setdefault(_pack_of(skill), []).append(skill)
    return sorted(groups.items(), key=lambda g: (g[0] != "", g[0].lower()))


class SkillPicker(PaletteRowsMixin, VerticalScroll):
    """Filtered overlay-список скиллов.

    Навигация (`set_filter` / `move_selection`), `compose` и `_rebuild` — общие
    с `ArgPicker`, в `PaletteRowsMixin`; `_row_widgets` свой — вставляет
    шапки пакетов, которых нет среди выбираемых `_visible_rows`."""

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

    def _format_row(self, skill: dict) -> Text:
        """Короткое имя (цвет — классом `.palette-skill-row`): под шапкой
        пакета — с отступом, без пакета — на месте шапки; затем серым
        описание с общей для всех строк позиции до правого края."""
        desc_x = len(_INDENT) + self._name_col_w()
        lead = _INDENT if _pack_of(skill) else _HEAD
        name = fit_name(skill_short(skill)).ljust(desc_x - len(lead))
        text = Text(lead + name)
        desc = str(skill.get("description") or "")
        width = self._line_width()
        if width:
            desc = fit_text(desc, max(1, width - desc_x))
        text.append(desc, style=_pal.DIM)
        return text

    def _matched(self) -> list[dict]:
        f = fold(self._filter)
        return [s for s in self._skills if f in fold(str(s.get("id", "")))]

    def _visible_groups(self) -> list[tuple[str, list[dict]]]:
        """Группы с совпадениями: пустая группа не показывает и шапку."""
        return _grouped(self._matched())

    def _visible_rows(self) -> list[dict]:
        """Выбираемые строки — только скиллы, в порядке групп."""
        return [s for _, group in self._visible_groups() for s in group]

    def _name_col_w(self) -> int:
        return name_col_w([skill_short(s) for s in self._visible_rows()])

    def _content_width(self) -> int:
        return overlay_content_width(self)

    def _line_width(self) -> int:
        """Ширина текста строки (внутри рамки и паддинга); вне приложения
        неизвестна — 0, описание не усекается."""
        width = self._content_width()
        return width - 2 if width else 0  # -2 — padding строки

    def _row_widgets(self) -> tuple[list[Static], Optional[Static]]:
        """Шапки пакетов (невыбираемые) и строки скиллов под ними; у
        группы без пакета шапки нет."""
        groups = self._visible_groups()
        if not groups:
            return super()._row_widgets()
        widgets: list[Static] = []
        selected: Optional[Static] = None
        idx = 0
        for pack, group in groups:
            if pack:
                widgets.append(
                    Static(
                        _HEAD + pack, classes="palette-pack-head", markup=False
                    )
                )
            for skill in group:
                classes = "palette-row palette-skill-row"
                if idx == self._selected:
                    classes += " selected"
                row = Static(self._format_row(skill), classes=classes)
                if idx == self._selected:
                    selected = row
                widgets.append(row)
                idx += 1
        return widgets, selected

    def _reveal(self, row: Static) -> None:
        """Выбранная строка в видимой области; первая строка списка — с
        самого верха, первый скилл группы — вместе с шапкой над ним
        (иначе при подъёме шапка остаётся за краем прокрутки)."""
        if row.parent is not self:  # строки уже пересобраны новым нажатием
            return
        if self._selected == 0:
            self.scroll_home(animate=False, immediate=True)
            return
        row.scroll_visible(animate=False, immediate=True)
        above = self.children[self.children.index(row) - 1]
        if above.has_class("palette-pack-head"):
            above.scroll_visible(animate=False, immediate=True)

    def _apply_size(self) -> None:
        """Textual не учитывает докнутых детей (`.palette-footer`, `dock: bottom`)
        в auto-высоте контейнера (см. подробный докстринг CommandPalette._apply_size) —
        без резервации подвал либо схлопывает контейнер, либо рисуется поверх хвоста
        списка. `min-height` поднимает итоговый размер до честного, не трогая
        declared `height: auto`."""
        self.styles.min_height = self.desired_height

    @property
    def desired_height(self) -> int:
        """Честная желаемая высота: строки списка с шапками пакетов (или одна
        no-match-строка) + докнутый footer (margin-top hint'а(1) + строка
        hint'а(1)) + рамка .palette(2).
        `_apply_size` ставит его в min-height — недосчёт выдавливал бы нижнюю
        рамку пикера за экран."""
        groups = self._visible_groups()
        flow = sum(bool(p) + len(g) for p, g in groups) if groups else 1
        return flow + 2 + 2

    def selected_skill(self) -> Optional[dict]:
        return self._selected_row()
