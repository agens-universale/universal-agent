"""Палитра команд — overlay над вводом (§2.7 спеки).

Toggle-подвал (/gate, /answers — спека 2026-07-16-palette-toggles-gate-answers-design
§4): докнутый вне скролла блок toggle-строк внизу списка, перед ним разделитель.
Состояние тоггла палитра не знает сама — получает инъекцией через `toggle_state`
(провайдер `name -> str`, отсутствует — если тоггл не инъецирован); запись/reload — вне палитры (tui/toggles.py).

Строки скиллов (спека P5 §3.1): после команд, с первой буквы фильтра;
совпадение — по короткому имени (по полному id — только если в фильтре есть
`:`). Бюджет строк общий: скиллам достаются строки, не занятые командами и
подвалом, но не меньше `_SKILL_ROWS_MIN`; остаток — невыбираемая строка
«… ещё N» (N >= 2: единственный скрытый скилл показывается). Список скиллов —
инъекция `skills` (провайдер без I/O, в проде — прогретый кэш экрана).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from rich.text import Text
from textual._context import NoActiveAppError
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.css.scalar import Unit
from textual.widgets import Rule, Static

from gc_tui import palette as _pal
from gc_tui.commands import Command, CommandRegistry
from gc_tui.toggles import _HINT_KEYS_TOGGLE, TOGGLES, _fit_hint, padded_track
from gc_tui.widgets.palette_layout import (
    fit_name,
    fit_text,
    name_col_w,
    overlay_content_width,
    skill_name_key,
    skill_short,
)

_HINT_NO_ARGS = "↑↓ выбрать · Enter выполнить · Esc закрыть"
_HINT_WITH_ARGS = "↑↓ выбрать · Enter ввести аргумент · Esc закрыть"
# Enter на строке скилла вставляет `/имя ` — текст дописывают (спека P5 §3.2).
_HINT_SKILL = "↑↓ выбрать · Enter ввести запрос · Esc закрыть"
_SKILL_ROWS_MIN = 5  # пол строк скиллов, даже когда команды заняли всё


def _slider_width(n_values: int) -> int:
    """Ширина поля слайдера в ячейках (спека 2026-07-18 §2): трек для ВСЕХ
    тогглов — 2*n-1 ячеек (глиф на каждую позицию + пробел-разделители между
    ними), независимо от числа значений."""
    return 2 * n_values - 1


def _hint_for(cmd: Command | None) -> str:
    """Хинт обычной (не toggle) строки: Enter подставит команду в поле — у
    команды есть что ввести или выбрать (`Command.runs_on_enter`), иначе —
    базовый hint. Toggle-строки получают свой hint через
    `CommandPalette._toggle_hint` (значение + подсказка из TOGGLES, спека §6)."""
    if cmd is not None and not cmd.runs_on_enter:
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


def _skill_label(skill: dict) -> str:
    """`/имя` для колонки: запускаемый вид от провайдера (`input_name`)."""
    return "/" + str(skill.get("input_name") or skill_short(skill))


def _skill_matches(skill: dict, prefix: str) -> bool:
    """Начало короткого имени; полного id — только при `:` в фильтре."""
    if ":" in prefix:
        return str(skill.get("id", "")).startswith(prefix)
    return skill_short(skill).startswith(prefix)


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
        toggle_values: Optional[Callable[[str], tuple]] = None,
        skills: Optional[Callable[[], list[dict]]] = None,
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
        # Динамический состав значений трека (спека автороутинга §3, /level) —
        # name -> ToggleValue-кортеж; не инъецирован (юнит-тесты без app) →
        # `_values_for` берёт статический `TOGGLES[name].values`.
        self._toggle_values = toggle_values
        self._skills = skills
        # Состав значений тогглов на время ОДНОГО рендера подвала (`_build_footer`):
        # раскладка и строки спрашивают его много раз, провайдер (/level) должен
        # отработать один. None — вне рендера, кэша нет.
        self._values_memo: Optional[dict] = None
        self._apply_size()  # честная высота с самого первого layout-прохода (см. _apply_size)

    def _values_for(self, name: str) -> tuple:
        """Состав значений трека тоггла `name`: инъецированный `toggle_values`
        (палитра в проде — `effective_toggle_values(app, name)`) либо
        статический `TOGGLES[name].values` без него. Внутри рендера подвала —
        один вызов провайдера на тоггл (`_values_memo`)."""
        memo = self._values_memo
        if memo is not None and name in memo:
            return memo[name]
        if self._toggle_values is not None:
            values = self._toggle_values(name)
        else:
            values = TOGGLES[name].values
        if memo is not None:
            memo[name] = values
        return values

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
            classes = (
                "palette-row selected"
                if i == self._selected
                else "palette-row"
            )
            yield Static(self._format_row(cmd), classes=classes, markup=False)
        skill_widgets, _ = self._skill_widgets(offset=len(rows))
        yield from skill_widgets
        offset = len(rows) + len(self._visible_skill_rows())
        footer, _ = self._build_footer(offset=offset)
        yield footer

    def _group_sep_count(self, toggles: list[Command]) -> int:
        """Число разделителей групп (ревизия 7, спека §4.1): один `Rule` перед
        КАЖДЫМ preset-родителем (`spec.preset_keys` непусто), который не
        первый в списке видимых toggle-строк — generic по `preset_keys`, не
        по конкретному имени. Сегодня в TOGGLES ровно одна preset-группа
        (`/rigor`) → 0; механизм рассчитан на будущие независимые группы.
        Общий источник для `_footer_reserved_height` и `_build_footer`."""
        return sum(
            1
            for j, c in enumerate(toggles)
            if j > 0 and TOGGLES[c.name].preset_keys
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
        skills_count = len(self._visible_skill_rows())
        more = 1 if self._skill_more_count() else 0
        return (
            rows_count
            + skills_count
            + more
            + self._footer_reserved_height()
            + 2  # рамка
        )

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
        self._values_memo = {}
        try:
            return self._build_footer_rows(offset)
        finally:
            self._values_memo = None

    def _build_footer_rows(
        self, offset: int
    ) -> tuple[Vertical, Optional[Static]]:
        toggles = self._visible_toggle_rows()
        children: list = []
        selected_row: Optional[Static] = None
        if toggles:
            children.append(Rule(classes="palette-separator"))
            for j, cmd in enumerate(toggles):
                if j > 0 and TOGGLES[cmd.name].preset_keys:
                    children.append(Rule(classes="palette-group-sep"))
                idx = offset + j
                classes = (
                    "palette-row palette-toggle-row selected"
                    if idx == self._selected
                    else "palette-row palette-toggle-row"
                )
                row = Static(
                    self._format_toggle_row(cmd), classes=classes, markup=False
                )
                if idx == self._selected:
                    selected_row = row
                children.append(row)
        children.append(
            Static(
                self._selected_hint(),
                classes="palette-hint palette-keys",
                markup=False,
            )
        )
        return Vertical(*children, classes="palette-footer"), selected_row

    def _selected_hint(self) -> str:
        """Подсказка клавиш для выбранной строки: тоггл, скилл, команда."""
        sel = self.selected_command()
        if sel is not None and sel.is_toggle:
            return self._toggle_hint(sel)
        if self.selected_skill() is not None:
            return _HINT_SKILL
        return _hint_for(sel)

    def _content_width(self) -> int:
        """Ширина контента `.palette` до первой раскладки (см.
        `overlay_content_width`): усечение по `self.size.width` на первом
        `compose()` молчало бы (Size(0, 0)), toggle-строка переносилась на
        2 строки, а `desired_height` считал одну — подвал уезжал за экран."""
        return overlay_content_width(self)

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
        current = self._current(cmd.name)
        width = self._content_width() - 4 if self._content_width() else 0
        if (
            spec.hint_provider is not None
            and self._toggle_dynamic_hint is not None
        ):
            hint = self._toggle_dynamic_hint(cmd.name, current)
            if not current:
                return hint
        elif current in spec.names:
            hint = spec.values[spec.names.index(current)].hint
        elif self._toggle_custom_hint is not None:
            hint = self._toggle_custom_hint(
                cmd.name
            )  # /rigor custom, отображаемый (спека §5)
        else:
            hint = spec.values[0].hint if spec.values else ""
        return _fit_hint(current, hint, _HINT_KEYS_TOGGLE, width)

    def _name_col_w(self) -> int:
        """Ширина имени-колонки, ОБЩЕЙ для команд, тогглов и скиллов (спека
        2026-07-18 §4): самое длинное видимое имя (`/имя`; у скилла — его
        запускаемый вид), не шире `SKILL_NAME_MAX`, + 3 пробела. Описания
        всех строк начинаются в одной колонке."""
        labels = [f"/{c.name}" for c in self._visible_rows()]
        labels += [f"/{c.name}" for c in self._visible_toggle_rows()]
        labels += [_skill_label(s) for s in self._visible_skill_rows()]
        return name_col_w(labels)

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
        """Ширина колонки хоткеев — самый длинный видимый хоткей, или 0, если
        их нет ни у одной видимой строки. Колонка прижата к правому краю
        строки (`_line_width`), хоткей не обрезается никогда."""
        return max((len(c.hotkey) for c in self._visible_rows()), default=0)

    def _line_width(self) -> int:
        """Ширина текста строки (внутри рамки и паддинга). Палитра вне
        приложения (юнит-тесты) её не знает — тогда берётся естественная
        ширина: самое длинное описание без усечения."""
        if self._content_width():
            return self._content_width() - 2  # -2 — padding строки
        rows = self._visible_rows()
        skills = self._visible_skill_rows()
        longest = max(
            [len(c.description) for c in rows]
            + [len(str(s.get("description") or "")) for s in skills],
            default=0,
        )
        return self._desc_start() + longest + self._hotkey_tail()

    def _desc_start(self) -> int:
        return 1 + self._name_col_w()  # ведущий пробел + имя-колонка

    def _hotkey_tail(self) -> int:
        """Ширина правого края строки: зазор 3 + колонка хоткеев (или 0)."""
        col = self._hotkey_col_w()
        return col + 3 if col else 0

    def _fit_desc(self, text: str, tail: int) -> str:
        """Описание, обрезанное «…» до места между колонкой имён и правым
        краем строки с хвостом шириной `tail`."""
        width = max(1, self._line_width() - self._desc_start() - tail)
        return fit_text(text, width)

    def _format_row(self, cmd: Command) -> str:
        head = f" {fit_name(f'/{cmd.name}').ljust(self._name_col_w())}"
        tail = self._hotkey_tail()
        desc = self._fit_desc(cmd.description, tail)
        if not tail or not cmd.hotkey:
            return head + desc  # хвост не нужен, колонку держат соседи
        gap = self._line_width() - len(head) - len(desc) - self._hotkey_col_w()
        return head + desc + " " * gap + cmd.hotkey.rjust(self._hotkey_col_w())

    def _format_skill_row(self, skill: dict) -> Text:
        """Строка скилла: запускаемое `/имя` в общей колонке имён (цвет —
        классом `.palette-skill-row`), длиннее `SKILL_NAME_MAX` — с
        многоточием; затем серым описание."""
        name = fit_name(_skill_label(skill))
        text = Text(f" {name.ljust(self._name_col_w())}")
        desc = self._fit_desc(
            str(skill.get("description") or ""), self._hotkey_tail()
        )
        text.append(desc, style=_pal.DIM)
        return text

    def _skill_widgets(
        self, offset: int
    ) -> tuple[list[Static], Optional[Static]]:
        """Виджеты строк скиллов (+ «ещё N») и выделенная строка или None."""
        widgets: list[Static] = []
        selected: Optional[Static] = None
        for j, skill in enumerate(self._visible_skill_rows()):
            is_sel = offset + j == self._selected
            classes = "palette-row palette-skill-row"
            row = Static(
                self._format_skill_row(skill),
                classes=f"{classes} selected" if is_sel else classes,
                markup=False,
            )
            if is_sel:
                selected = row
            widgets.append(row)
        if self._skill_more_count():
            widgets.append(
                Static(
                    self._skill_more_text(),
                    classes="palette-skill-more",
                    markup=False,
                )
            )
        return widgets, selected

    def _my_base_w(self, name: str) -> int:
        """Собственная ширина БАЗОВОГО трека тоггла `name` (без extra-резерва),
        по числу ON-SCALE значений ДЕЙСТВУЮЩЕГО состава (`_values_for` — статика
        или `values_provider`, спека автороутинга §3). `show_track=False`
        (спека §12.1, /profiles — trackless) → 0: нет трека, нечего
        резервировать."""
        spec = TOGGLES[name]
        if not spec.show_track:
            return 0
        on_scale = sum(1 for v in self._values_for(name) if not v.off_scale)
        return _slider_width(on_scale)

    def _my_extra_w(self, name: str) -> int:
        """Собственная ширина extra-зоны тоггла `name`: тогглы с вне-шкальными
        значениями действующего состава (спека автороутинга §3, /level —
        `auto`) резервируют `2 + 2*k - 1` ячеек (двойной пробел + k глифов,
        разделённых пробелом; k=1 → 3); иначе — 0, если `toggle_extra` не
        задействован (`None`, обычный single-key тоггл), 3 — custom-резерв
        /rigor (спека §6). `show_track=False` (/profiles) → 0 сразу, как у
        `_my_base_w`: провайдер значений trackless-тоггла раскладке не нужен."""
        if not TOGGLES[name].show_track:
            return 0
        off_scale = sum(1 for v in self._values_for(name) if v.off_scale)
        if off_scale:
            return 2 + 2 * off_scale - 1
        extra = (
            self._toggle_extra(name)
            if self._toggle_extra is not None
            else None
        )
        return 3 if extra is not None else 0

    def _current(self, name: str) -> str:
        """Применённое значение тоггла `name` (без инъекции — первое)."""
        if self._toggle_state is not None:
            return self._toggle_state(name)
        names = TOGGLES[name].names
        return names[0] if names else ""

    def _value_text(self, name: str) -> str:
        """Подпись значения в строке: пустое — словами спеки («без
        профиля»), не прочерк."""
        return self._current(name) or TOGGLES[name].empty_value_text

    def _toggle_block_w(
        self, toggles: list[Command], track_w: int, label_w: int
    ) -> int:
        """Ширина правого блока тоггл-строк от общей колонки `col`: трек +
        2 + подпись, у trackless (/profiles) — само значение. Общая для всех
        строк — значение /profiles начинается там же, где треки."""
        widths = [
            track_w + 2 + label_w
            if TOGGLES[c.name].show_track
            else len(self._value_text(c.name))
            for c in toggles
        ]
        return max(widths)

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
        Task 2). Окно уже раскладки — `col` сдвигается влево (описание
        усекается), правый блок (`_toggle_block_w`) не режется никогда."""
        toggles = self._visible_toggle_rows()
        name_w = self._name_col_w()
        col = max(len(self._row_head(c, name_w)) for c in toggles) + 3
        base_w = max(self._my_base_w(c.name) for c in toggles)
        extra_w = max(self._my_extra_w(c.name) for c in toggles)
        label_w = max(
            (
                len(n)
                for c in toggles
                if TOGGLES[c.name].show_track
                for n in (
                    *(v.value for v in self._values_for(c.name)),
                    self._current(c.name),
                )
            ),
            default=0,
        )
        block_w = self._toggle_block_w(toggles, base_w + extra_w, label_w)
        if self._content_width():
            # имя-колонка + хотя бы 1 символ описания + зазор 3
            floor = 1 + name_w + 1 + 3
            col = max(floor, min(col, self._line_width() - block_w))
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
        current = self._current(cmd.name)
        col, base_w, extra_w, _ = self._toggle_columns()
        is_child = self._owning_preset(cmd.name) is not None
        head = self._row_head(cmd)
        if len(head) > col - 3:  # окно уже раскладки — режется описание
            head = head[: col - 4] + "…"

        if not spec.show_track:
            # Trackless (спека §12.1, /profiles): без шкалы — сразу значение-имя
            # в общей колонке трека (`col`), без слайдера/резерва
            # (произвольное число профилей — шкала не имеет смысла).
            text = Text(head.ljust(col))
            text.append(self._value_text(cmd.name))
            if is_child:
                text.stylize(_pal.SUB_INK)
            return text

        text = Text(head.ljust(col))
        if is_child:
            text.stylize(_pal.SUB_INK, 0, len(head))
        extra = (
            self._toggle_extra(cmd.name)
            if self._toggle_extra is not None
            else None
        )
        values = self._values_for(cmd.name)
        text.append_text(
            padded_track(
                spec,
                current,
                extra_lit=extra,
                base_w=base_w,
                extra_w=extra_w,
                values=values,
            )
        )
        text.append("  ")
        value_start = len(text)
        text.append(current)
        if is_child:
            text.stylize(_pal.SUB_INK, value_start, len(text))
        return text

    def _visible_rows(self) -> list[Command]:
        """Верхняя (скроллируемая) зона: обычные команды, без тогглов."""
        return self._filtered(toggle=False)

    def _visible_toggle_rows(self) -> list[Command]:
        """Toggle-подвал: только is_toggle-команды."""
        return self._filtered(toggle=True)

    def _matched_skills(self) -> list[dict]:
        """Все скиллы под фильтр (голый `/` — ни одного); скилл, чьё короткое
        имя занято командой или алиасом, не показывается. Порядок — по
        короткому имени (`skill_name_key`), как в пикере /skill."""
        f = self._filter.lower()
        if not f or self._skills is None:
            return []
        matched = [
            s
            for s in self._skills()
            if _skill_matches(s, f)
            and self._registry.lookup(skill_short(s)) is None
        ]
        return sorted(matched, key=skill_name_key)

    def _list_rows_cap(self) -> int:
        """Сколько строк списка палитра показывает без прокрутки: потолок
        высоты (`styles.max_height` в клетках — его выставляет main.py под
        экран; до этого — половина экрана, как там) минус рамка и подвал."""
        cap = self.styles.max_height
        if cap is not None and cap.unit == Unit.CELLS:
            height = int(cap.value)
        else:
            try:
                height = self.app.size.height // 2
            except NoActiveAppError:
                height = 0
        return height - 2 - self._footer_reserved_height()

    def _visible_skill_rows(self) -> list[dict]:
        """Строки скиллов после команд в рамках общего бюджета. При
        переполнении одна строка бюджета отдана под «ещё N», но не меньше
        пола; единственный скрытый скилл показывается вместо «ещё 1»."""
        matched = self._matched_skills()
        free = self._list_rows_cap() - len(self._visible_rows())
        if len(matched) <= free:
            return matched
        shown = max(_SKILL_ROWS_MIN, free - 1)
        if len(matched) - shown == 1:
            return matched
        return matched[:shown]

    def _skill_more_count(self) -> int:
        return len(self._matched_skills()) - len(self._visible_skill_rows())

    def _skill_more_text(self) -> str:
        n = self._skill_more_count()
        return f"… ещё {n} — /skill {self._filter}"

    def _filtered(self, *, toggle: bool) -> list[Command]:
        f = self._filter.lower()
        cmds = [
            c for c in self._registry.list_commands() if c.is_toggle == toggle
        ]
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
        base = (
            [c for c in cmds if _matches(c, f)]
            if f
            else [c for c in cmds if TOGGLES[c.name].palette_default]
        )
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

    def _selectable_rows(self) -> list[Command | dict]:
        """Сквозной список для навигации/выбора: команды, скиллы, затем тогглы
        (разделитель и «ещё N» не участвуют — они не выбираемы)."""
        return (
            self._visible_rows()
            + self._visible_skill_rows()
            + self._visible_toggle_rows()
        )

    def _selected_row(self) -> Command | dict | None:
        rows = self._selectable_rows()
        if not rows:
            return None
        return rows[max(0, min(self._selected, len(rows) - 1))]

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
        """Выбранная команда; на строке скилла — None."""
        row = self._selected_row()
        return row if isinstance(row, Command) else None

    def selected_skill(self) -> Optional[dict]:
        """Запись выбранного скилла; на строке команды — None."""
        row = self._selected_row()
        return row if isinstance(row, dict) else None

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
            classes = (
                "palette-row selected"
                if i == self._selected
                else "palette-row"
            )
            w = Static(self._format_row(cmd), classes=classes, markup=False)
            if i == self._selected:
                selected_widget = w
            new_widgets.append(w)
        skill_widgets, skill_selected = self._skill_widgets(offset=len(rows))
        new_widgets.extend(skill_widgets)
        offset = len(rows) + len(self._visible_skill_rows())
        footer, footer_selected = self._build_footer(offset=offset)
        new_widgets.append(footer)
        for candidate in (skill_selected, footer_selected):
            if candidate is not None:
                selected_widget = candidate
        self._apply_size()
        self.remove_children()
        self.mount_all(new_widgets)
        if selected_widget is not None:
            self.call_after_refresh(
                selected_widget.scroll_visible, animate=False
            )
