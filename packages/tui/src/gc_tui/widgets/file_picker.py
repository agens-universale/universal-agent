"""FilePicker — overlay-список путей для @-аттача. Зеркало SkillPicker.

Отличие: фильтрацию/листинг делает экран (через file_index), сюда приходят
готовые строки путей. Те же CSS-классы (.palette / .palette-row / .palette-hint)."""
from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

_HINT = "↑↓ выбрать · Tab/Enter дополнить · Esc закрыть"
_NO_MATCH = "(нет совпадений)"
_SEARCHING = "ищу по проекту…"
# Не длиннее _HINT: высота подвала считается по одной строке на подсказку.
_TRUNCATED = "поиск неполный · уточните путь: папка/файл"
# То же правило длины: укладывается в _HINT до миллиона записей в папке включительно.
_LISTING_CUT = "показано {shown} из {total} · допишите часть имени"


class FilePicker(VerticalScroll):
    """Filtered overlay-список путей (строки; папки с хвостовым '/')."""

    DEFAULT_CSS = ""  # стили — .palette / .palette-row / .palette-hint в styles.tcss

    def __init__(self, entries: list[str], *, truncated: bool = False,
                 searching: bool = False, total: Optional[int] = None) -> None:
        super().__init__(classes="palette")
        self._entries = list(entries)
        # Индекс путей упёрся в предохранитель (file_index.MAX_INDEX_PATHS): без
        # подсказки «файл есть, а пикер молчит» выглядит как сломанный поиск.
        self._truncated = truncated
        # Долгий фоновый поиск ещё не вернулся: пустая строка показывает
        # «ищу по проекту…» вместо «нет совпадений» (см. _empty_text).
        self._searching = searching
        # Листинг папки обрезан потолком строк: сколько записей в ней ВСЕГО.
        # None — неизвестно/не листинг (поиск по индексу уже обрезает сам список).
        self._total = total
        self._selected = 0
        self._stale = False   # данные сменились до конца монтирования: перестроить в on_mount
        self._rows: list[Static] = []   # ссылки на строки-виджеты — для move_selection без пересоздания
        self._apply_size()  # честная высота с самого первого layout-прохода (см. _apply_size)

    def _empty_text(self) -> str:
        return _SEARCHING if self._searching else _NO_MATCH

    def _note_text(self) -> str:
        """Строка над клавишами: индекс обрезан предохранителем — совет про путь;
        листинг папки обрезан потолком строк — сколько показано и что делать. Вместе
        не бывают: первое — про поиск по проекту, второе — про листинг."""
        if self._truncated:
            return _TRUNCATED
        if self._total is not None and self._total > len(self._entries):
            return _LISTING_CUT.format(shown=len(self._entries), total=self._total)
        return ""

    def compose(self) -> ComposeResult:
        if not self._entries:
            self._rows = []
            yield Static(self._empty_text(), classes="palette-hint", markup=False)
        else:
            rows = []
            for i, entry in enumerate(self._entries):
                classes = "palette-row selected" if i == self._selected else "palette-row"
                w = Static(f" {entry}", classes=classes, markup=False)
                rows.append(w)
                yield w
            self._rows = rows
        yield self._build_footer()

    def _build_footer(self) -> Vertical:
        """Докнутый подвал (паттерн CommandPalette._build_footer): hint — ПОСЛЕДНИЙ
        FLOW-ребёнок в старой раскладке упирался в max-height-клэмп длинного списка
        и прятался за скроллом (регрессия воздуха .palette-keys, коммит c0622c9a).
        Докнутый контейнер вне скролла держит hint видимым при открытии всегда."""
        children = []
        note = self._note_text()
        if note:
            # Канон ArgPicker: динамическая строка — над клавишами, со своим воздухом.
            children.append(Static(note, classes="palette-hint palette-keys", markup=False))
        children.append(Static(_HINT, classes="palette-hint palette-keys", markup=False))
        return Vertical(*children, classes="palette-footer")

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
        докнутый footer (margin-top hint'а(1) + строка hint'а(1), при обрезанном
        индексе или обрезанном потолком строк листинге — ещё margin(1) +
        строка-подсказка(1)) + рамка .palette(2).
        main.py::_sync_trace_max_height берёт это значение напрямую — недосчёт
        выдавливал бы нижнюю рамку пикера за экран."""
        flow = len(self._entries) if self._entries else 1
        footer = 2 + (2 if self._note_text() else 0)
        return flow + footer + 2

    def set_entries(self, entries: list[str], *, truncated: bool = False,
                     searching: bool = False, total: Optional[int] = None) -> None:
        self._entries = list(entries)
        self._truncated = truncated
        self._searching = searching
        self._total = total
        self._selected = 0
        self._rebuild()

    def move_selection(self, delta: int) -> None:
        if not self._entries:
            return
        old = self._selected
        self._selected = (self._selected + delta) % len(self._entries)
        if len(self._rows) != len(self._entries):
            self._rebuild()                     # строки ещё не те — старый путь
            return
        self._rows[old].remove_class("selected")
        row = self._rows[self._selected]
        row.add_class("selected")
        if self.is_mounted:
            self.call_after_refresh(row.scroll_visible, animate=False)

    def selected_entry(self) -> Optional[str]:
        if not self._entries:
            return None
        idx = max(0, min(self._selected, len(self._entries) - 1))
        return self._entries[idx]

    def on_mount(self) -> None:
        # is_mounted становится True только ПОСЛЕ обработки Mount — прямо отсюда
        # _rebuild снова ушёл бы в ветку «ещё монтируемся».
        if self._stale:
            self._stale = False
            self.call_after_refresh(self._rebuild)

    def _rebuild(self) -> None:
        if not self.is_mounted:
            # Ещё монтируемся: compose мог уже отработать по прежним данным.
            # Высоту чиним сразу, строки — как только смонтируемся.
            self._apply_size()
            self._stale = True
            return
        new_widgets: list = []
        selected_widget: Optional[Static] = None
        if not self._entries:
            self._rows = []
            new_widgets.append(Static(self._empty_text(), classes="palette-hint", markup=False))
        else:
            rows = []
            for i, entry in enumerate(self._entries):
                classes = "palette-row selected" if i == self._selected else "palette-row"
                w = Static(f" {entry}", classes=classes, markup=False)
                if i == self._selected:
                    selected_widget = w
                rows.append(w)
                new_widgets.append(w)
            self._rows = rows
        new_widgets.append(self._build_footer())
        self._apply_size()
        self.remove_children()
        self.mount_all(new_widgets)
        if selected_widget is not None:
            self.call_after_refresh(selected_widget.scroll_visible, animate=False)
