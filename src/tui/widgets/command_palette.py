"""Палитра команд — overlay над вводом (§2.7 спеки).

Toggle-подвал (/gate, /answers — спека 2026-07-16-palette-toggles-gate-answers-design
§4): докнутый вне скролла блок toggle-строк внизу списка, перед ним разделитель.
Состояние тоггла палитра не знает сама — получает инъекцией через `toggle_state`
(провайдер `name -> str`, отсутствует — если тоггл не инъецирован); запись/reload — вне палитры (tui/toggles.py).
"""
from __future__ import annotations

from typing import Callable, Optional

from rich.text import Text
from textual._context import NoActiveAppError
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Rule, Static

from tui.commands import Command, CommandRegistry
from tui.toggles import TOGGLES, _fit_hint, _HINT_KEYS_TOGGLE, render_track

_HINT_NO_ARGS = "↑↓ выбрать · Enter выполнить · Esc закрыть"
_HINT_WITH_ARGS = "↑↓ выбрать · Enter ввести аргумент · Esc закрыть"


def _slider_width(n_values: int) -> int:
    """Ширина поля слайдера в ячейках (спека 2026-07-18 §2): трек для ВСЕХ
    тогглов — 2*n-1 ячеек (глиф на каждую позицию + пробел-разделители между
    ними), независимо от числа значений."""
    return 2 * n_values - 1


def _hint_for(cmd: Command | None) -> str:
    """Хинт обычной (не toggle) строки: есть схема args → Enter подставит команду
    в поле, иначе — базовый hint. Toggle-строки получают свой hint через
    `CommandPalette._toggle_hint` (значение + подсказка из TOGGLES, спека §6)."""
    if cmd is not None and cmd.args:
        return _HINT_WITH_ARGS
    return _HINT_NO_ARGS


class CommandPalette(VerticalScroll):
    """Filtered overlay-список команд из реестра (§2.7) + toggle-подвал (§4)."""

    DEFAULT_CSS = ""  # стили в styles.tcss (.palette / .palette-row / .palette-footer / ...)

    def __init__(
        self,
        registry: CommandRegistry,
        initial_filter: str = "",
        toggle_state: Optional[Callable[[str], str]] = None,
    ) -> None:
        super().__init__(classes="palette")
        self._registry = registry
        self._filter = initial_filter
        self._selected = 0
        self._toggle_state = toggle_state
        self._apply_size()  # честная высота с самого первого layout-прохода (см. _apply_size)

    def compose(self) -> ComposeResult:
        rows = self._visible_rows()
        for i, cmd in enumerate(rows):
            classes = "palette-row selected" if i == self._selected else "palette-row"
            yield Static(self._format_row(cmd), classes=classes, markup=False)
        footer, _ = self._build_footer(offset=len(rows))
        yield footer

    def _footer_reserved_height(self) -> int:
        """Высота докнутого `.palette-footer` (в строках): разделитель(1) + N
        toggle-строк, если есть (§4 «пустой подвал прячется вместе с разделителем»),
        всегда — margin-top(1) hint'а + сама строка hint'а(1). Синхронно с составом
        `_build_footer`."""
        toggles = self._visible_toggle_rows()
        hint_block = 2  # .palette-keys margin-top(1) + строка hint'а(1)
        if not toggles:
            return hint_block
        return 1 + len(toggles) + hint_block  # разделитель(1) + N строк + hint_block

    def _apply_size(self) -> None:
        """Честная высота контейнера: `.palette` держит `height: auto` (важно для
        `test_palette_hugs_content` — рамка обнимает контент, а не растянута), но
        Textual при auto-высоте не учитывает докнутых детей (`.palette-footer`,
        `dock: bottom`) — без резервации подвал либо схлопывает контейнер до одной
        рамки (пустая верхняя зона — фильтр не матчит ни одной обычной команды),
        либо рисуется поверх последних строк верхней зоны (полный список длиннее
        подвала): docked-child не резервирует себе место в потоке сам.
        `min-height` НЕ трогает declared `height` (остаётся auto), но поднимает
        итоговый резолвленный размер, если auto-расчёт вышел меньше — верхняя зона
        + подвал + рамка. `padding-bottom` для той же цели не годится: Textual
        считает dock-якорь докнутого ребёнка от `container_size` (content-box,
        уже за вычетом padding), поэтому padding не резервирует место снизу, а
        наоборот утапливает якорь дальше вверх (вплоть до отрицательных смещений).
        `max-height` (CSS `.palette` 80% или `styles.max_height`, выставленный
        извне из main.py под текущий экран) продолжает клэмпить сверху как обычно —
        на зажатом экране подвал всё ещё может перекрыть хвост списка (см.
        существующий `test_overlay_capped_at_half_screen`), это отдельный,
        принятый trade-off скролла, не регрессия этого фикса."""
        self.styles.min_height = self.desired_height

    @property
    def desired_height(self) -> int:
        """Честная желаемая высота контейнера (строки + докнутый подвал + рамка) —
        та же формула, что и в `_apply_size` (min-height). Публично наружу для
        main.py::_sync_trace_max_height: там `len(children)+2` — эвристика для
        плоских оверлеев (SkillPicker/ArgPicker/FilePicker, где каждый ребёнок —
        строка), но для палитры дети overlay = N строк + ОДИН докнутый
        `.palette-footer`-контейнер (внутри — несколько строк подвала), поэтому
        эвристика недосчитывает высоту подвала и main.py отдаёт трейсу лишнее
        место, выдавливая нижнюю рамку палитры за экран."""
        rows_count = len(self._visible_rows())
        return rows_count + self._footer_reserved_height() + 2  # +2 — рамка

    def _build_footer(self, offset: int) -> tuple[Vertical, Optional[Static]]:
        """Докнутый подвал: разделитель + toggle-строки (только если есть toggle-команды
        — правило §4 «пустой подвал прячется вместе с разделителем») и hint ПОСЛЕДНИМ
        элементом. Hint живёт внутри докнутого контейнера, а не рядом с ним снаружи —
        иначе он выпадает из потока раньше подвала и рендерится выше него (dock: bottom
        вырывает только сам контейнер, не соседей по yield-порядку). Возвращает
        (контейнер для монтирования, выделенная toggle-строка или None) — второе нужно
        _rebuild для scroll_visible (children контейнера пусты до реального mount)."""
        toggles = self._visible_toggle_rows()
        children: list = []
        selected_row: Optional[Static] = None
        if toggles:
            children.append(Rule(classes="palette-separator"))
            for j, cmd in enumerate(toggles):
                idx = offset + j
                classes = "palette-row palette-toggle-row selected" if idx == self._selected \
                    else "palette-row palette-toggle-row"
                row = Static(self._format_toggle_row(cmd), classes=classes, markup=False)
                if idx == self._selected:
                    selected_row = row
                children.append(row)
        sel = self.selected_command()
        hint_text = self._toggle_hint(sel) if sel is not None and sel.is_toggle else _hint_for(sel)
        children.append(Static(hint_text, classes="palette-hint palette-keys", markup=False))
        return Vertical(*children, classes="palette-footer"), selected_row

    def _content_width(self) -> int:
        """Ширина контента `.palette` (за вычетом рамки), известная ДО первого
        layout-прохода: `self.size` у ещё не смонтированного/не уложенного
        виджета — Size(0, 0) (Textual резолвит его только после arrange),
        поэтому усечение по `self.size.width` на первом `compose()` молчит
        (avail=0 → guard пропускает усечение) и toggle-строка рендерится
        нетронутой длины — переносится на 2 строки в реальном рендере, а
        `desired_height`/`_footer_reserved_height()` считали 1 строку на
        тоггл → подвал недосчитан, палитра/футер уезжают за экран (регрессия).
        `.palette` всегда занимает всю ширину `#root` (= ширина экрана,
        см. `_mount_input_overlay`), поэтому ширина известна заранее из
        `self.app.size`, а не из собственного ещё не уложенного `self.size`.
        Юнит-тесты собирают `CommandPalette` без монтирования в приложение
        (нет активного `App`) — тогда ширина неизвестна, как и раньше у
        `self.size.width` на несмонтированном виджете (0)."""
        try:
            app_width = self.app.size.width
        except NoActiveAppError:
            return 0
        return app_width - 2  # -2 — рамка .palette (round border)

    def _toggle_hint(self, cmd: Command) -> str:
        """Hint выделенной toggle-строки (спека §6): значение + его смысл из
        TOGGLES для ПРИМЕНЁННОГО значения (черновика не существует — модель
        мгновенная), приоритетно усечённые под ширину подвала (`_fit_hint`)."""
        spec = TOGGLES[cmd.name]
        if self._toggle_state is not None:
            current = self._toggle_state(cmd.name)
        else:
            current = spec.names[0]
        idx = spec.names.index(current) if current in spec.names else 0
        val = spec.values[idx]
        width = self._content_width() - 4 if self._content_width() else 0
        return _fit_hint(val.value, val.hint, _HINT_KEYS_TOGGLE, width)

    def _name_col_w(self) -> int:
        """Ширина имени-колонки (спека 2026-07-18 §4): описания ВСЕХ строк
        палитры (обычных и тоггловых) начинаются в общей колонке — имя
        `/{cmd.name}` паддируется до самого длинного имени среди видимых
        строк ОБЕИХ зон (обычные команды + тогглы) + 3 пробела."""
        cmds = self._visible_rows() + self._visible_toggle_rows()
        if not cmds:
            return 0
        return max(len(f"/{c.name}") for c in cmds) + 3

    def _row_head(self, cmd: Command, name_w: Optional[int] = None) -> str:
        """Строка "имя-колонка + описание" с общей шириной имени-колонки (§4).
        Обычные строки рендерятся этим как есть; тоггл-строки используют её
        как основу перед слайдером (`_format_toggle_row`)."""
        if name_w is None:
            name_w = self._name_col_w()
        return f" {f'/{cmd.name}'.ljust(name_w)}{cmd.description}"

    def _format_row(self, cmd: Command) -> str:
        return self._row_head(cmd)

    def _toggle_columns(self) -> tuple[int, int, int]:
        """(col, track_w, label_w) — общая раскладка слайдеров тоггл-подвала
        (спека 2026-07-18 §4). `col` — колонка, с которой слайдер начинается
        ОДИНАКОВО во всех тоггл-строках: длина самой длинной ВЫРОВНЕННОЙ
        строки "имя-колонка+описание" (`_row_head`, общая имя-колонка по ОБЕИМ
        зонам) среди ВИДИМЫХ тогглов + 3 пробела. `track_w` — ширина общего
        поля слайдера: самый широкий слайдер (`_slider_width`) среди них —
        короткие слайдеры (включая 1-ячеечный индикатор) внутри поля
        прижимаются вправо в `_format_toggle_row`. `label_w` — ширина колонки
        подписи значения: самое длинное имя значения среди ВСЕХ видимых
        тогглов (не только текущей строки) — иначе усечение узкого терминала
        в `_format_toggle_row` считает РАЗНЫЙ head_w на разных строках и
        слайдеры съезжают по колонкам между ними (находка ревью Task 2)."""
        toggles = self._visible_toggle_rows()
        name_w = self._name_col_w()
        col = max(len(self._row_head(c, name_w)) for c in toggles) + 3
        track_w = max(_slider_width(len(TOGGLES[c.name].values)) for c in toggles)
        label_w = max(len(n) for c in toggles for n in TOGGLES[c.name].names)
        return col, track_w, label_w

    def _format_toggle_row(self, cmd: Command) -> Text:
        """Слайдер-рендер тоггл-строки (спека 2026-07-18 §2-4): трек для ВСЕХ
        тогглов (2*n-1 ячеек), глиф на позицию каждого значения — ползунок
        `●` цветом СВОЕГО значения (семантика, не позиция), пустые позиции —
        незакрашенный `○` (DEEP_DIM). Колонки слайдера/подписи общие для всех
        тоггл-строк подвала. Применённое значение всегда — модель мгновенная
        (§1), черновика нет. Подпись значения — без своего style (наследует
        цвет строки — `.palette-row` уже красит текст в INK, см. styles.tcss)."""
        spec = TOGGLES[cmd.name]
        if self._toggle_state is not None:
            current = self._toggle_state(cmd.name)
        else:
            current = spec.names[0]

        name_w = self._name_col_w()
        col, track_w, label_w = self._toggle_columns()
        head = self._row_head(cmd, name_w)

        # Узкий терминал (§4): слайдер+подпись не жертвуются — усекаем описание.
        # head_w/col считаем от ОБЩИХ track_w/label_w и общего prefix_floor
        # (не от этой строки) — тогда усечённый col тоже общий для всех
        # тоггл-строк подвала (раньше label_w брался из spec.names ТЕКУЩЕЙ
        # команды, и head_w/col получались per-row — треки съезжали по
        # колонкам между строками, находка ревью Task 2). Имя-колонка теперь
        # общая по обеим зонам (`_name_col_w`), поэтому отступ до описания
        # (" " + имя.ljust(name_w)) одинаков для всех тоггл-строк — floor
        # константен, не считается per-row.
        avail = self._content_width() - 2 if self._content_width() else 0
        need = col + track_w + 2 + label_w
        if avail and need > avail:
            prefix_floor = 1 + name_w + 1  # отступ имени-колонки + хотя бы 1 символ описания
            head_w = max(prefix_floor, avail - (track_w + 2 + label_w) - 3)
            if len(head) > head_w:
                head = head[: head_w - 1] + "…"
            col = head_w + 3

        text = Text(head.ljust(col))
        my_w = _slider_width(len(spec.values))
        text.append(" " * (track_w - my_w))  # слайдер прижат вправо в общем поле
        text.append_text(render_track(spec, current))
        text.append("  ")
        text.append(current)
        return text

    def _visible_rows(self) -> list[Command]:
        """Верхняя (скроллируемая) зона: обычные команды, без тогглов."""
        return self._filtered(toggle=False)

    def _visible_toggle_rows(self) -> list[Command]:
        """Toggle-подвал: только is_toggle-команды."""
        return self._filtered(toggle=True)

    def _filtered(self, *, toggle: bool) -> list[Command]:
        f = self._filter.lower()
        cmds = [c for c in self._registry.list_commands() if c.is_toggle == toggle]
        if not f:
            return cmds
        return [c for c in cmds if c.name.startswith(f)]

    def _selectable_rows(self) -> list[Command]:
        """Сквозной список для навигации/выбора: команды, затем тогглы (разделитель
        не участвует — он не выбираем)."""
        return self._visible_rows() + self._visible_toggle_rows()

    def set_filter(self, prefix: str) -> None:
        self._filter = prefix
        self._selected = 0
        self._rebuild()

    def move_selection(self, delta: int) -> None:
        rows = self._selectable_rows()
        if not rows:
            return
        self._selected = (self._selected + delta) % len(rows)
        self._rebuild()

    def selected_command(self) -> Optional[Command]:
        rows = self._selectable_rows()
        if not rows:
            return None
        idx = max(0, min(self._selected, len(rows) - 1))
        return rows[idx]

    def refresh_toggle_state(self) -> None:
        """Перерисовать строки после мгновенного применения тоггла (§1): конфиг
        изменился вне палитры (apply_toggle), сама она об этом не узнаёт —
        роутинг клавиш (main.py) зовёт явно, чтобы строка/hint отразили новое
        значение."""
        self._rebuild()

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
        footer, footer_selected = self._build_footer(offset=len(rows))
        new_widgets.append(footer)
        if footer_selected is not None:
            selected_widget = footer_selected
        self._apply_size()
        self.remove_children()
        self.mount_all(new_widgets)
        if selected_widget is not None:
            self.call_after_refresh(selected_widget.scroll_visible, animate=False)
