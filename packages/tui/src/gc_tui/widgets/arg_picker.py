"""ArgPicker — overlay-список значений аргумента команды.

Зеркало `SkillPicker`/`CommandPalette`: те же CSS-классы (.palette /
.palette-row / .palette-hint) и модель состояния (filter + selected index).
Опции — `PickerOption` (value, описание, статус, знак слева, доступность,
черта); старые кортежи `(value, desc)` (`/export`) и `(value, desc, status)`
(`/profile`) нормализуются в него же. Строка со статусом или знаком —
`Text`: знак, имя, колонка статуса сразу за именем (цветом OK, ✗-отказ —
ERR; у строк без статуса место пустое), описание последним — `SUB_INK`, оно
и ужимается при нехватке ширины; без статуса и знака — простой `str`. Черта (`separator`) не выбирается и видна, только если по
обе её стороны есть видимые строки; недоступная строка (`enabled=False`)
видна приглушённой, Enter её не берёт."""

from __future__ import annotations

import textwrap
from typing import Callable, NamedTuple, Optional

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Rule, Static

from gc_tui import palette as _pal
from gc_tui.widgets.palette_rows import PaletteRowsMixin

# Строка клавиш: Enter запускает действие (`/export`) или только выбирает
# значение (`/model`, `/profile` — `Command.arg_picks`).
RUN_KEYS_HINT = "↑↓ выбрать · Tab дополнить · Enter выполнить · Esc закрыть"
PICK_KEYS_HINT = "↑↓ выбрать · Tab дополнить · Enter выбрать · Esc закрыть"
# Выбор закрыт (`Command.arg_lock_provider`): листать можно, причина — в
# строке клавиш вместо обещания Enter.
LOCKED_KEYS_HINT = "↑↓ смотреть · Esc закрыть · {reason}"
# Сколько горит причина после Enter на невыбираемом.
FLASH_SECONDS = 2.0


class PickerOption(NamedTuple):
    """Строка пикера: значение, описание, статус за именем, знак слева."""

    value: str
    desc: str
    status: str = ""
    lead: Optional[Text] = None
    enabled: bool = True
    separator: bool = False


def _as_option(opt: tuple) -> PickerOption:
    return opt if isinstance(opt, PickerOption) else PickerOption(*opt)


class ArgPicker(PaletteRowsMixin, VerticalScroll):
    """Filtered overlay-список значений аргумента.

    Навигация (`set_filter` / `move_selection`), `compose` и `_rebuild` — общие
    с `SkillPicker`, в `PaletteRowsMixin`."""

    DEFAULT_CSS = (
        ""  # стили — .palette / .palette-row / .palette-hint в styles.tcss
    )

    def __init__(
        self,
        options: list[tuple],
        initial_filter: str = "",
        hint_provider: Optional[Callable[[Optional[str]], str]] = None,
        *,
        initial_value: Optional[str] = None,
        keys_hint: Optional[str] = RUN_KEYS_HINT,
        lock_provider: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        super().__init__(classes="palette")
        self._options = [_as_option(opt) for opt in options]
        self._filter = initial_filter
        # Строка клавиш подвала; None — не рисуется (хозяин пикера сам
        # говорит о клавишах, и чужие обещания здесь соврали бы).
        self._keys_hint = keys_hint
        # Причина, по которой выбор сейчас закрыт (None — открыт); хозяин
        # перечитывает её через `sync_lock`.
        self._lock_provider = lock_provider
        self._lock = lock_provider() if lock_provider else None
        # Enter на невыбираемом: строка с причиной (клавиш при замке, хинт
        # недоступной строки) горит WARN, пока не истёк таймер или не
        # сдвинулся курсор.
        self._flash = False
        self._flash_timer = None
        self._selected = self._start_index(initial_value)
        # Динамический хинт подсвеченного пункта (спека §12.1, /profile: состав
        # слотов профиля) — строкой над статичной клавиш-подсказкой подвала.
        self._hint_provider = hint_provider
        # Тексты хинтов по значению (см. `_hint_text`): место под хинт
        # резервируется по всем видимым строкам, и провайдер (у /profile —
        # проба сборки конфига) не должен звучать на каждый ход стрелкой.
        self._hint_texts: dict[Optional[str], str] = {}
        self._hint_static: Optional[Static] = None
        self._apply_size()  # честная высота с самого первого layout-прохода (см. _apply_size)

    def _keys_renderable(self) -> str | Text | None:
        """Строка клавиш с рендером: причина закрытого выбора — WARN, пока
        горит ответ на Enter; высота не меняется (текст тот же)."""
        line = self.keys_line()
        if line is None or not (self._flash and self._lock is not None):
            return line
        text = Text(line)
        text.stylize(_pal.WARN, len(line) - len(self._lock))
        return text

    def _hint_renderable(self) -> str | Text:
        """Хинт подсвеченной строки: причина недоступной строки — WARN,
        пока горит ответ на Enter."""
        text = self._hint_text(self.selected_value())
        if self._flash and self._lock is None:
            warn = Text(text)
            warn.stylize(_pal.WARN)
            return warn
        return text

    def flash_reason(self) -> None:
        """Ответ на Enter на невыбираемом: подсветить строку с причиной
        (замок — строка клавиш, недоступная строка — её хинт). Причины
        нет — гореть нечему."""
        if self._lock is None and not self._hint_text(self.selected_value()):
            return
        self._set_flash(True)
        self._flash_timer = self.set_timer(
            FLASH_SECONDS, lambda: self._set_flash(False)
        )

    def _set_flash(self, on: bool) -> None:
        if self._flash_timer is not None:
            self._flash_timer.stop()
            self._flash_timer = None
        if self._flash != on:
            self._flash = on
            self._rebuild()

    def _build_footer(self) -> Vertical:
        """Докнутый подвал (паттерн CommandPalette._build_footer): hint — ПОСЛЕДНИЙ
        FLOW-ребёнок в старой раскладке упирался в max-height-клэмп длинного списка
        и прятался за скроллом (регрессия воздуха .palette-keys, коммит c0622c9a).
        Докнутый контейнер вне скролла держит hint видимым при открытии всегда."""
        children = []
        if self._hint_provider is not None:
            # palette-keys и у динамического хинта: воздух над ним, отделяющий
            # хинт от строк списка (ревизия 2026-07-28 по живому смоку).
            hint = Static(
                self._hint_renderable(),
                classes="palette-hint palette-keys",
                markup=False,
            )
            # Высота — резерв под самый длинный хинт видимых строк: ход
            # стрелкой не двигает ни подвал, ни рамку.
            hint.styles.height = self._hint_lines()
            self._hint_static = hint
            children.append(hint)
        if self._keys_hint is not None:
            children.append(
                Static(
                    self._keys_renderable(),
                    classes="palette-hint palette-keys",
                    markup=False,
                )
            )
        return Vertical(*children, classes="palette-footer")

    def _choice_rows(self) -> list[PickerOption]:
        return [o for o in self._visible_rows() if not o.separator]

    def _value_col_w(self) -> int:
        """Ширина value-колонки: value паддируется до самого длинного value
        среди ВИДИМЫХ (отфильтрованных) опций + 3 пробела (то же правило
        выравнивания, что и в CommandPalette, спека 2026-07-18 §4)."""
        rows = self._choice_rows()
        if not rows:
            return 0
        return max(len(opt.value) for opt in rows) + 3

    def _rich_rows(self) -> bool:
        """Есть ли у видимых строк статус, знак или недоступность: тогда
        строки — `Text` с общими колонками описания и статуса."""
        return any(
            o.status or o.lead or not o.enabled for o in self._choice_rows()
        )

    def _status_col_w(self) -> int:
        """Ширина колонки статуса: самый широкий статус среди ВИДИМЫХ строк
        + 3 пробела; статусов нет — колонки нет."""
        widths = [len(o.status) for o in self._choice_rows() if o.status]
        return max(widths) + 3 if widths else 0

    def _lead_col_w(self) -> int:
        """Ширина колонки знака: по самому широкому знаку среди видимых."""
        leads = [len(o.lead) for o in self._choice_rows() if o.lead]
        return max(leads, default=0)

    def _lead_text(self, opt: PickerOption) -> Text:
        """Знак прижат вправо, к значению, один пробел до него."""
        width = self._lead_col_w()
        if not width:
            return Text()
        lead = opt.lead or Text()
        return Text.assemble(" " * (width - len(lead)), lead, " ")

    def _format_row(self, opt: tuple) -> str | Text:
        """Строка списка: без статуса и знака у всех видимых строк — простой
        `str` (другие пикеры — /export и т.п. — не должны заметить правку);
        иначе `Text`: знак, значение, статус, описание. Недоступная строка:
        знак цветной, остальное приглушено."""
        opt = _as_option(opt)
        if not self._rich_rows():
            if not opt.desc:
                return f" {opt.value}"
            return f" {opt.value.ljust(self._value_col_w())}{opt.desc}"
        t = Text(" ")
        t.append_text(self._lead_text(opt))
        body_start = len(t.plain)
        if not opt.desc and not opt.status:
            t.append(opt.value)
        else:
            t.append(opt.value.ljust(self._value_col_w()))
            self._append_status(t, opt.status)
            t.append(opt.desc, style=_pal.SUB_INK)
        if not opt.enabled:
            # Знак сохраняет свои цвета, приглушены имя, статус, описание.
            t = Text.assemble(
                t[:body_start], Text(t.plain[body_start:], style=_pal.DIM)
            )
        return t

    def _append_status(self, t: Text, status: str) -> None:
        """Статус в своей колонке. Цвет — по смыслу: ✗-отказ («не
        применится») зелёным цветом достижения читался бы как «всё в
        порядке». Правило по глифу, а не по литералу: пикер не знает ни про
        профили, ни про их тексты."""
        width = self._status_col_w()
        if not width:
            return
        if status:
            t.append(
                status,
                style=_pal.ERR if status.startswith("✗") else _pal.OK,
            )
        t.append(" " * (width - len(status)))

    def _row_widget(self, row: PickerOption, classes: str) -> Static | Rule:
        if row.separator:
            return Rule(classes="palette-separator")
        return Static(self._format_row(row), classes=classes, markup=False)

    def _visible_rows(self) -> list[PickerOption]:
        """Опции под фильтр; черта — только между видимыми строками."""
        f = self._filter.strip().lower()
        out: list[PickerOption] = []
        pending: Optional[PickerOption] = None
        for opt in self._options:
            if opt.separator:
                pending = opt if out else None
                continue
            if f and not opt.value.lower().startswith(f):
                continue
            if pending is not None:
                out.append(pending)
                pending = None
            out.append(opt)
        return out

    def move_selection(self, delta: int) -> None:
        """Шаг по кругу, перешагивая черту."""
        rows = self._visible_rows()
        if not rows:
            return
        idx = self._selected
        for _ in rows:
            idx = (idx + delta) % len(rows)
            if not rows[idx].separator:
                break
        self._selected = idx
        self._set_flash(False)
        self._rebuild()

    def _apply_size(self) -> None:
        """Textual не учитывает докнутых детей (`.palette-footer`, `dock: bottom`)
        в auto-высоте контейнера (см. подробный докстринг CommandPalette._apply_size) —
        без резервации подвал либо схлопывает контейнер, либо рисуется поверх хвоста
        списка. `min-height` поднимает итоговый размер до честного, не трогая
        declared `height: auto`."""
        self.styles.min_height = self.desired_height

    def _reveal_selected(self) -> None:
        rows = self.query(".palette-row.selected")
        if rows:
            self._reveal(rows.first())

    def _reveal(self, row: Static) -> None:
        """Подсвеченная строка — в видимой части списка, сам пикер — в
        видимой части хозяина (в «Настройках» он стоит в прокрутке
        секции, и строка без подвала была бы видна наполовину)."""
        row.scroll_visible(animate=False)
        self.scroll_visible(animate=False)

    def on_resize(self) -> None:
        """Ширина приходит с раскладкой (до неё хинт считается одной
        строкой): резерв хинта и высота пикера — по фактической ширине."""
        if self._hint_static is not None:
            self._hint_static.styles.height = self._hint_lines()
        self._apply_size()

    def watch_virtual_size(self) -> None:
        """Раскладка сменила размер содержимого — строки или подвал:
        докрутить подсвеченную строку по свежей геометрии. Так стартовая
        подсветка (`initial_value` в хвосте длинного списка) видна сразу,
        и подвал, выросший под переносы хинта уже после первой раскладки,
        её не накрывает. Прокрутка колесом размер не меняет — с ней
        докрутка не спорит."""
        self._reveal_selected()

    def _footer_line_count(self, text: str) -> int:
        """Сколько СТРОК займёт строка подвала при текущей ширине.

        Не «одна», как считалось раньше: `text-wrap: nowrap` стоит только у
        `.palette-row`, а `.palette-hint` переносится. Причина отказа профиля
        (путь + текст исключения, 130+ символов) не влезает в строку даже при 120
        колонках, а клавиш-подсказка перестаёт влезать к 60.

        Ширина текста = ширина пикера минус рамка (2) и `padding: 0 1` (2). До
        первого layout-прохода ширины ещё нет — считаем по одной строке; с
        шириной пересчёт делает `on_resize`, а `_apply_size` зовётся заново и на
        каждой перерисовке (`_rebuild`).

        Модель переноса — `textwrap` (жадная укладка по словам, длинное слово
        ломается), а не деление длины на ширину: делением можно ЗАНИЗИТЬ, а занижение
        и есть баг — подвал рисуется выше выданного места и накрывает хвост списка."""
        width = self.size.width - 4
        if width < 1 or not text:
            return 1
        return max(1, len(textwrap.wrap(text, width)))

    def _hint_text(self, value: Optional[str]) -> str:
        """Текст провайдера для `value`, посчитанный не чаще раза на значение
        за жизнь пикера: и резерв подвала, и сам подвал спрашивают хинты
        снова на каждой перерисовке."""
        if self._hint_provider is None:
            return ""
        if value not in self._hint_texts:
            self._hint_texts[value] = self._hint_provider(value) or ""
        return self._hint_texts[value]

    def _hint_lines(self) -> int:
        """Строк под хинт — по самому длинному хинту ВИДИМЫХ строк, а не
        подсвеченной: подвал не прыгает при смене выбора и пересчитывается
        только со сменой фильтра (или ширины)."""
        values = [o.value for o in self._choice_rows()] or [None]
        return max(
            self._footer_line_count(self._hint_text(value)) for value in values
        )

    @property
    def desired_height(self) -> int:
        """Честная желаемая высота: строки списка (или одна no-match-строка) +
        докнутый footer (у каждой его строки — свой margin-top(1) и СВОИ строки
        после переноса; у хинта — резерв `_hint_lines`) + рамка .palette(2).
        `_apply_size` ставит его в min-height — недосчёт выдавливал бы нижнюю
        рамку пикера за экран."""
        rows = self._visible_rows()
        flow = len(rows) if rows else 1
        footer = 0
        if self._keys_hint is not None:
            footer += 1 + self._footer_line_count(self.keys_line())
        if self._hint_provider is not None:
            footer += 1 + self._hint_lines()
        return flow + 2 + footer

    def keys_line(self) -> Optional[str]:
        """Строка клавиш подвала: при закрытом выборе — «смотреть» и
        причина."""
        if self._lock is not None and self._keys_hint is not None:
            return LOCKED_KEYS_HINT.format(reason=self._lock)
        return self._keys_hint

    def sync_lock(self) -> None:
        """Перечитать замок выбора; сменился — перерисовать подвал."""
        if self._lock_provider is None:
            return
        lock = self._lock_provider()
        if lock != self._lock:
            self._lock = lock
            self._flash = False
            self._rebuild()

    def selected_value(self) -> Optional[str]:
        row = self._selected_row()
        if row is None:
            return None
        return row.value

    def selected_disabled(self) -> bool:
        """Подсвечена видимая недоступная строка или выбор закрыт: Enter
        строку не берёт. Нет видимых строк — не блок: Enter уходит с
        набранным аргументом (и команда отвечает своим отказом)."""
        row = self._selected_row()
        return row is not None and (not row.enabled or self._lock is not None)

    def _first_enabled(self) -> int:
        """Стартовая подсветка — первая доступная строка (не `auto`, когда
        автовыбор недоступен); нет такой — первая."""
        rows = self._visible_rows()
        return next(
            (i for i, o in enumerate(rows) if o.enabled and not o.separator),
            0,
        )

    def _start_index(self, value: Optional[str]) -> int:
        """Стартовая подсветка — строка `value` (текущее значение), если
        она видна и доступна; иначе первая доступная."""
        rows = self._visible_rows()
        return next(
            (
                i
                for i, o in enumerate(rows)
                if value is not None
                and o.value == value
                and o.enabled
                and not o.separator
            ),
            self._first_enabled(),
        )

    def set_filter(self, text: str) -> None:
        self._filter = text
        self._selected = self._first_enabled()
        self._set_flash(False)
        self._rebuild()
