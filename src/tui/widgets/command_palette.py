"""Палитра команд — overlay над вводом (§2.7 спеки).

Toggle-подвал (/gate, /answers — спека 2026-07-16-palette-toggles-gate-answers-design
§4): докнутый вне скролла блок toggle-строк внизу списка, перед ним разделитель.
Состояние тоггла палитра не знает сама — получает инъекцией через `toggle_state`
(провайдер `name -> str`, отсутствует — если тоггл не инъецирован); запись/reload — вне палитры (tui/toggles.py).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from rich.text import Text
from textual._context import NoActiveAppError
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Rule, Static

from tui import palette as _pal
from tui.commands import Command, CommandRegistry
from tui.toggles import TOGGLES, _fit_hint, _HINT_KEYS_TOGGLE, padded_track

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


def _matches(cmd: Command, prefix: str) -> bool:
    """Фильтр строки палитры: имя ИЛИ любой алиас начинается с набранного
    (спека §12.1 — тихий алиас `models`→`/level`: набравший «/models» находит
    `/level` уже на этапе фильтрации, не только по прямому вводу полного
    алиаса). Generic по `Command.aliases` — тот же механизм, что у `/сжать`
    (`/compact`), просто применённый и к живому фильтру, не только к `lookup`."""
    if cmd.name.startswith(prefix):
        return True
    return any(alias.startswith(prefix) for alias in cmd.aliases)


class CommandPalette(VerticalScroll):
    """Filtered overlay-список команд из реестра (§2.7) + toggle-подвал (§4)."""

    DEFAULT_CSS = ""  # стили в styles.tcss (.palette / .palette-row / .palette-footer / ...)

    def __init__(
        self,
        registry: CommandRegistry,
        initial_filter: str = "",
        toggle_state: Optional[Callable[[str], str]] = None,
        toggle_extra: Optional[Callable[[str], Any]] = None,
        toggle_custom_hint: Optional[Callable[[str], str]] = None,
        toggle_dynamic_hint: Optional[Callable[[str, str], str]] = None,
    ) -> None:
        super().__init__(classes="palette")
        self._registry = registry
        self._filter = initial_filter
        self._selected = 0
        self._toggle_state = toggle_state
        self._toggle_extra = toggle_extra
        self._toggle_custom_hint = toggle_custom_hint
        # Динамический hint (спека §12.1, /level и /profiles): (name, value) -> текст,
        # заменяет статический `ToggleValue.hint` для тогглов с `hint_provider`.
        self._toggle_dynamic_hint = toggle_dynamic_hint
        self._apply_size()  # честная высота с самого первого layout-прохода (см. _apply_size)

    def _toggle_children(self, name: str) -> list[str]:
        """Дети пресета `name` (спека §4.1): тоггл-имена, чей `dotted` входит в
        его `preset_keys`, в порядке `preset_keys`. Generic по данным TOGGLES —
        не знает про /rigor/gate/answers по имени."""
        spec = TOGGLES[name]
        by_dotted = {s.dotted: n for n, s in TOGGLES.items() if s.dotted}
        return [by_dotted[d] for d in spec.preset_keys if d in by_dotted]

    def _owning_preset(self, name: str) -> Optional[str]:
        """Имя preset-тоггла, чей ребёнок — `name`, иначе `None` (ревизия 7,
        спека §2). Чистое data-родство по `preset_keys` — БЕЗ гейта видимости
        родителя (ревизия 6 его несла как условие DIM/отступа; ревизия 7 сняла
        видимость-по-выделению вовсе — серый сигнал ребёнка действует ВЕЗДЕ,
        включая одиночного ребёнка, найденного фильтром при невидимом
        родителе)."""
        for preset_name, spec in TOGGLES.items():
            if not spec.preset_keys:
                continue
            if name in self._toggle_children(preset_name):
                return preset_name
        return None

    def compose(self) -> ComposeResult:
        rows = self._visible_rows()
        for i, cmd in enumerate(rows):
            classes = "palette-row selected" if i == self._selected else "palette-row"
            yield Static(self._format_row(cmd), classes=classes, markup=False)
        footer, _ = self._build_footer(offset=len(rows))
        yield footer

    def _group_sep_count(self, toggles: list[Command]) -> int:
        """Число разделителей групп (ревизия 7, спека §4.1): один `Rule` перед
        КАЖДЫМ preset-родителем (`spec.preset_keys` непусто), который не
        первый в списке видимых toggle-строк — generic по `preset_keys`, не
        по конкретному имени. Сегодня в TOGGLES ровно одна preset-группа
        (`/rigor`) → 0; механизм рассчитан на будущие независимые группы.
        Общий источник для `_footer_reserved_height` и `_build_footer`."""
        return sum(
            1 for j, c in enumerate(toggles) if j > 0 and TOGGLES[c.name].preset_keys
        )

    def _footer_reserved_height(self) -> int:
        """Высота докнутого `.palette-footer` (в строках): разделитель(1) + N
        toggle-строк + разделители групп между ними, если есть (§4 «пустой
        подвал прячется вместе с разделителем»), всегда — margin-top(1)
        hint'а + сама строка hint'а(1). Синхронно с составом `_build_footer`."""
        toggles = self._visible_toggle_rows()
        hint_block = 2  # .palette-keys margin-top(1) + строка hint'а(1)
        if not toggles:
            return hint_block
        # разделитель(1) + N строк + разделители групп + hint_block
        return 1 + len(toggles) + self._group_sep_count(toggles) + hint_block

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
        вырывает только сам контейнер, не соседей по yield-порядку). Разделитель ГРУППЫ
        (ревизия 7, спека §4.1) — `Rule(classes="palette-group-sep")` перед КАЖДЫМ
        preset-родителем, который не первая toggle-строка (`_group_sep_count`, тот же
        источник, что и `_footer_reserved_height`). Возвращает (контейнер для
        монтирования, выделенная toggle-строка или None) — второе нужно _rebuild для
        scroll_visible (children контейнера пусты до реального mount)."""
        toggles = self._visible_toggle_rows()
        children: list = []
        selected_row: Optional[Static] = None
        if toggles:
            children.append(Rule(classes="palette-separator"))
            for j, cmd in enumerate(toggles):
                if j > 0 and TOGGLES[cmd.name].preset_keys:
                    children.append(Rule(classes="palette-group-sep"))
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
        мгновенная), приоритетно усечённые под ширину подвала (`_fit_hint`).
        Применённое значение вне `spec.names` (ревизия 4 — отображаемый
        custom, custom сам не выбирается) — хинт берёт динамический провайдер
        `toggle_custom_hint` (спека §5, два состояния).

        `hint_provider` (спека §12.1, /level и /profiles) — динамический hint
        вместо статического `ToggleValue.hint`, читается через инъекцию
        `toggle_dynamic_hint`. Пустое состояние /profiles (`current` пусто,
        профилей нет) — hint возвращается ЦЕЛИКОМ, без обёртки
        «значение — …  ·  клавиши»: цикла без позиций нет, обёртка была бы
        бессмысленной (канон §12.1 — текст пустого состояния дословно)."""
        spec = TOGGLES[cmd.name]
        if self._toggle_state is not None:
            current = self._toggle_state(cmd.name)
        else:
            current = spec.names[0] if spec.names else ""
        width = self._content_width() - 4 if self._content_width() else 0
        if spec.hint_provider is not None and self._toggle_dynamic_hint is not None:
            hint = self._toggle_dynamic_hint(cmd.name, current)
            if not current:
                return hint
        elif current in spec.names:
            hint = spec.values[spec.names.index(current)].hint
        elif self._toggle_custom_hint is not None:
            hint = self._toggle_custom_hint(cmd.name)     # /rigor custom, отображаемый (спека §5)
        else:
            hint = spec.values[0].hint if spec.values else ""
        return _fit_hint(current, hint, _HINT_KEYS_TOGGLE, width)

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
        как основу перед слайдером (`_format_toggle_row`). Flat (ревизия 7) —
        никакого отступа: иерархия ребёнок/родитель несётся только цветом
        (DIM), не позицией."""
        if name_w is None:
            name_w = self._name_col_w()
        return f" {f'/{cmd.name}'.ljust(name_w)}{cmd.description}"

    def _hotkey_col_w(self) -> int:
        """Колонка, с которой начинаются хоткеи, или 0 — их нет ни у одной видимой
        строки. Считается по самому длинному видимому описанию (тот же приём, что у
        имени): иначе хоткеи вставали бы в хвост строк разной длины лесенкой.

        Зазор +3, как и у колонки описаний (§4): было +2, и на строке с самым длинным
        описанием рядом оказывались два разных зазора — 3 слева от описания и 2 справа.
        На одной строке это видно глазом (находка живой приёмки)."""
        rows = [c for c in self._visible_rows()]
        if not any(c.hotkey for c in rows):
            return 0
        # По ВСЕМ видимым описаниям, а не только по строкам с хоткеем: у длинного
        # описания без клавиши иначе не осталось бы зазора вовсе.
        return max(len(c.description) for c in rows) + 3

    def _format_row(self, cmd: Command) -> str:
        head = self._row_head(cmd)
        col = self._hotkey_col_w()
        if not col or not cmd.hotkey:
            return head              # колонку держит паддинг соседей, пустой хвост не нужен
        return head + " " * (col - len(cmd.description)) + cmd.hotkey

    def _my_base_w(self, name: str) -> int:
        """Собственная ширина БАЗОВОГО трека тоггла `name` (без extra-резерва).
        `show_track=False` (спека §12.1, /profiles — trackless) → 0: нет трека,
        нечего резервировать."""
        spec = TOGGLES[name]
        if not spec.show_track:
            return 0
        return _slider_width(len(spec.values))

    def _my_extra_w(self, name: str) -> int:
        """Собственная ширина extra-зоны тоггла `name`: 0 — extra не
        задействован (`toggle_extra` не инъецирован или вернул `None`, обычный
        single-key тоггл); иначе фиксированные 3 ячейки (двойной пробел +
        глиф custom-резерва, спека §6)."""
        extra = self._toggle_extra(name) if self._toggle_extra is not None else None
        return 3 if extra is not None else 0

    def _toggle_columns(self) -> tuple[int, int, int, int]:
        """(col, base_w, extra_w, label_w) — общая раскладка тоггл-подвала
        (спека 2026-07-18 §4, выравнивание — ревизия 5 §6). `col` — колонка,
        с которой начинается трек ОДИНАКОВО во всех тоггл-строках: длина
        самой длинной ВЫРОВНЕННОЙ строки "имя-колонка+описание" (`_row_head`,
        общая имя-колонка по ОБЕИМ зонам) среди ВИДИМЫХ тогглов + 3 пробела.
        `base_w`/`extra_w` — общее поле трека РАЗДЕЛЬНО по зонам (левый край,
        не единое право-прижатое поле, как было): самый широкий базовый трек
        и самый широкий extra-резерв среди видимых строк — короткие базовые
        треки (включая 1-ячеечный индикатор) паддятся пробелами СПРАВА в
        `_format_toggle_row` (через `padded_track`, tui/toggles.py), custom-
        резерв — правее троек, тоже паддится справа. `label_w` — ширина
        колонки подписи значения: самое длинное имя значения среди ВСЕХ
        видимых тогглов (не только текущей строки) — иначе усечение узкого
        терминала в `_format_toggle_row` считает РАЗНЫЙ head_w на разных
        строках и слайдеры съезжают по колонкам между ними (находка ревью
        Task 2)."""
        toggles = self._visible_toggle_rows()
        name_w = self._name_col_w()
        col = max(len(self._row_head(c, name_w)) for c in toggles) + 3
        base_w = max(self._my_base_w(c.name) for c in toggles)
        extra_w = max(self._my_extra_w(c.name) for c in toggles)
        label_w = max((len(n) for c in toggles for n in TOGGLES[c.name].names), default=0)
        return col, base_w, extra_w, label_w

    def _format_toggle_row(self, cmd: Command) -> Text:
        """Слайдер-рендер тоггл-строки (спека 2026-07-18 §2-4, выравнивание —
        ревизия 5 §6, flat/серый — ревизия 7 §2): трек для ВСЕХ тогглов (2*n-1
        ячеек), глиф на позицию каждого значения — ползунок `●` цветом
        СВОЕГО значения (семантика, не позиция), пустые позиции —
        незакрашенный `○` (DEEP_DIM). Колонки слайдера/подписи общие для всех
        тоггл-строк подвала, выровнены по ЛЕВОМУ краю (`padded_track`,
        tui/toggles.py). Применённое значение всегда — модель мгновенная
        (§1), черновика нет. Подпись значения — без своего style (наследует
        цвет строки — `.palette-row` уже красит текст в INK, см. styles.tcss).
        Ребёнок preset-тоггла (`_owning_preset`, ревизия 7 — видим ВСЕГДА, без
        отступа) — имя и описание стилем DIM (весь `head`), И подпись
        значения тоже DIM (у детей — только их слайдер семантическим цветом,
        никакого другого акцента); трек/подпись в тех же общих колонках, что
        и у всех тоггл-строк."""
        spec = TOGGLES[cmd.name]
        if self._toggle_state is not None:
            current = self._toggle_state(cmd.name)
        else:
            current = spec.names[0] if spec.names else ""

        name_w = self._name_col_w()
        col, base_w, extra_w, label_w = self._toggle_columns()
        is_child = self._owning_preset(cmd.name) is not None
        head = self._row_head(cmd, name_w)

        if not spec.show_track:
            # Trackless (спека §12.1, /profiles): без шкалы — сразу значение-имя
            # на месте начала общей колонки трека (`col`), без слайдера/резерва
            # (произвольное число профилей — шкала не имеет смысла). Пустое
            # значение — словами спеки («без профиля»), не прочерк.
            value_text = current or spec.empty_value_text
            avail = self._content_width() - 2 if self._content_width() else 0
            need = col + len(value_text)
            if avail and need > avail:
                prefix_floor = 1 + name_w + 1
                head_w = max(prefix_floor, avail - len(value_text) - 3)
                if len(head) > head_w:
                    head = head[: head_w - 1] + "…"
                col = head_w + 3
            text = Text(head.ljust(col))
            text.append(value_text)
            if is_child:
                text.stylize(_pal.DIM)
            return text

        track_w = base_w + extra_w

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
            prefix_floor = 1 + name_w + 1  # имя-колонка + хотя бы 1 символ описания
            head_w = max(prefix_floor, avail - (track_w + 2 + label_w) - 3)
            if len(head) > head_w:
                head = head[: head_w - 1] + "…"
            col = head_w + 3

        text = Text(head.ljust(col))
        if is_child:
            text.stylize(_pal.DIM, 0, len(head))
        extra = self._toggle_extra(cmd.name) if self._toggle_extra is not None else None
        text.append_text(padded_track(spec, current, extra_lit=extra, base_w=base_w, extra_w=extra_w))
        text.append("  ")
        value_start = len(text)
        text.append(current)
        if is_child:
            text.stylize(_pal.DIM, value_start, len(text))
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
        if not toggle:
            # Обычные команды — фильтр как был: startswith по имени ИЛИ алиасу
            # (§12.1: тихий алиас `models`→`/level` — тот же приём здесь).
            return [c for c in cmds if _matches(c, f)] if f else cmds
        # Toggle-подвал (ревизия 7, спека §2, §4.1): база — пустой фильтр =
        # `palette_default`-тогглы, непустой = startswith (то же правило, что
        # и раньше). Для КАЖДОГО preset-родителя из базы сразу после него
        # вставляются его дети (`_toggle_children`, резолвящиеся в реестре),
        # ВСЕГДА — без условия выделения (композиция статична, высота не
        # скачет при навигации).
        #
        # Дедуп: если ребёнок совпал с фильтром САМ (уже присутствует в базе
        # на своей естественной позиции) — не добавляется второй раз
        # родительской вставкой; рисуется ОДИН раз, сразу после родителя, как
        # ребёнок группы (не отдельной строкой на своём месте в базе).
        # `owned` предвычисляется ДО основного прохода — порядко-независимо
        # (находка ревью Z1): если Command ребёнка зарегистрирован в реестре
        # РАНЬШЕ родителя, наивная отметка "injected по факту вставки" ещё
        # не сработала бы к моменту встречи ребёнка на его собственной
        # позиции в базе → дубликат. `owned` знает про родство заранее (по
        # ЛЮБОМУ preset-родителю, реально присутствующему в базе), поэтому
        # прямая встреча ребёнка в базе пропускается независимо от порядка
        # регистрации — он всё равно будет вставлен инъекцией под настоящим
        # родителем. Ребёнок, чей родитель фильтром НЕ пойман (отсутствует в
        # базе), в `owned` не попадает — остаётся обычной строкой на своём месте.
        base = [c for c in cmds if _matches(c, f)] if f else \
            [c for c in cmds if TOGGLES[c.name].palette_default]
        owned: set[str] = set()
        for c in base:
            if TOGGLES[c.name].preset_keys:
                owned.update(self._toggle_children(c.name))
        injected: set[str] = set()
        result: list[Command] = []
        for c in base:
            if c.name in owned:
                continue
            result.append(c)
            spec = TOGGLES[c.name]
            if not spec.preset_keys:
                continue
            for child_name in self._toggle_children(c.name):
                if child_name in injected:
                    continue
                child = self._registry.lookup(child_name)
                if child is None:
                    continue
                result.append(child)
                injected.add(child_name)
        return result

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
