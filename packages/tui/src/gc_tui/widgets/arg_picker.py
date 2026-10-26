"""ArgPicker — overlay-список значений аргумента команды.

Зеркало `SkillPicker`/`CommandPalette`: те же CSS-классы (.palette /
.palette-row / .palette-hint) и модель состояния (filter + selected index).
Опции — `PickerOption` (value, описание, статус справа, знак слева,
доступность, черта); старые кортежи `(value, desc)` (`/export`) и
`(value, desc, status)` (`/profiles`) нормализуются в него же. Строка со
статусом или знаком — `Text` (статус цветом OK, ✗-отказ — ERR); без них —
простой `str`. Черта (`separator`) не выбирается и видна, только если по
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

_HINT = "↑↓ выбрать · Tab дополнить · Enter выполнить · Esc закрыть"


class PickerOption(NamedTuple):
    """Строка пикера: значение, описание, статус справа, знак слева."""

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
        keys_hint: Optional[str] = _HINT,
    ) -> None:
        super().__init__(classes="palette")
        self._options = [_as_option(opt) for opt in options]
        self._filter = initial_filter
        # Строка клавиш подвала; None — не рисуется (хозяин пикера сам
        # говорит о клавишах, и чужие обещания здесь соврали бы).
        self._keys_hint = keys_hint
        self._selected = self._start_index(initial_value)
        # Динамический хинт подсвеченного пункта (спека §12.1, /profiles: состав
        # слотов профиля) — строкой над статичной клавиш-подсказкой подвала.
        self._hint_provider = hint_provider
        # Кэш последнего посчитанного хинта — на ОДИН выбор (см. `_hint_text`):
        # `_build_footer` и `desired_height` спрашивали провайдер каждый по-своему
        # — без кэша полная проба (у /profiles — сборка конфига целиком) шла
        # дважды на каждый выбор ради текста одного и того же пункта.
        self._hint_cache: Optional[tuple] = None
        self._apply_size()  # честная высота с самого первого layout-прохода (см. _apply_size)

    def _build_footer(self) -> Vertical:
        """Докнутый подвал (паттерн CommandPalette._build_footer): hint — ПОСЛЕДНИЙ
        FLOW-ребёнок в старой раскладке упирался в max-height-клэмп длинного списка
        и прятался за скроллом (регрессия воздуха .palette-keys, коммит c0622c9a).
        Докнутый контейнер вне скролла держит hint видимым при открытии всегда."""
        children = []
        if self._hint_provider is not None:
            # palette-keys и у динамического хинта: воздух над ним, отделяющий
            # хинт от строк списка (ревизия 2026-07-28 по живому смоку).
            children.append(
                Static(
                    self._hint_text(self.selected_value()),
                    classes="palette-hint palette-keys",
                    markup=False,
                )
            )
        if self._keys_hint is not None:
            children.append(
                Static(
                    self._keys_hint,
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

    def _desc_col_w(self) -> int:
        """Ширина description-колонки у строк со статусом: описание паддится
        до самого длинного описания среди ВИДИМЫХ строк + 3 пробела — статус
        (правая колонка) стартует в одном месте у всех строк."""
        descs = [o.desc for o in self._choice_rows()]
        if not descs or not any(descs):
            return 0
        return max(len(d) for d in descs) + 3

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
        иначе `Text`: знак, значение, описание и правый статус цветом OK
        (✗-отказ — ERR). Недоступная строка: знак цветной, остальное
        приглушено."""
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
            t.append(opt.desc.ljust(self._desc_col_w()))
        if opt.status:
            # Цвет — по смыслу статуса: ✗-отказ («не применится») зелёным
            # цветом достижения читался бы как «всё в порядке». Правило по
            # глифу, а не по литералу: пикер не знает ни про профили, ни про
            # их тексты.
            t.append(
                opt.status,
                style=_pal.ERR if opt.status.startswith("✗") else _pal.OK,
            )
        if not opt.enabled:
            # Знак сохраняет свои цвета, приглушены имя, описание, статус.
            t = Text.assemble(
                t[:body_start], Text(t.plain[body_start:], style=_pal.DIM)
            )
        return t

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
        self._rebuild()

    def _apply_size(self) -> None:
        """Textual не учитывает докнутых детей (`.palette-footer`, `dock: bottom`)
        в auto-высоте контейнера (см. подробный докстринг CommandPalette._apply_size) —
        без резервации подвал либо схлопывает контейнер, либо рисуется поверх хвоста
        списка. `min-height` поднимает итоговый размер до честного, не трогая
        declared `height: auto`."""
        self.styles.min_height = self.desired_height

    def _footer_line_count(self, text: str) -> int:
        """Сколько СТРОК займёт строка подвала при текущей ширине.

        Не «одна», как считалось раньше: `text-wrap: nowrap` стоит только у
        `.palette-row`, а `.palette-hint` переносится. Причина отказа профиля
        (путь + текст исключения, 130+ символов) не влезает в строку даже при 120
        колонках, а клавиш-подсказка перестаёт влезать к 60.

        Ширина текста = ширина пикера минус рамка (2) и `padding: 0 1` (2). До
        первого layout-прохода ширины ещё нет — считаем по одной строке, тем же
        ответом, что и раньше; `_apply_size` зовётся заново на каждой перерисовке
        (`_rebuild`), а `main.py` спрашивает высоту уже после `call_after_refresh`.

        Модель переноса — `textwrap` (жадная укладка по словам, длинное слово
        ломается), а не деление длины на ширину: делением можно ЗАНИЗИТЬ, а занижение
        и есть баг — подвал рисуется выше выданного места и накрывает хвост списка."""
        width = self.size.width - 4
        if width < 1 or not text:
            return 1
        return max(1, len(textwrap.wrap(text, width)))

    def _hint_text(self, value: Optional[str]) -> str:
        """Текст провайдера для `value`, посчитанный не чаще раза на выбор.

        И `_build_footer`, и `desired_height` спрашивают хинт подсвеченного
        пункта. Кэш на одну запись
        (значение → текст): смена подсветки — новый счёт, тот же пункт в том же
        или следующем кадре — прежний ответ без второго вызова провайдера."""
        if self._hint_provider is None:
            return ""
        if self._hint_cache is not None and self._hint_cache[0] == value:
            return self._hint_cache[1]
        text = self._hint_provider(value) or ""
        self._hint_cache = (value, text)
        return text

    @property
    def desired_height(self) -> int:
        """Честная желаемая высота: строки списка (или одна no-match-строка) +
        докнутый footer (у каждой его строки — свой margin-top(1) и СВОИ строки
        после переноса) + рамка .palette(2).
        `_apply_size` ставит его в min-height — недосчёт выдавливал бы нижнюю
        рамку пикера за экран."""
        rows = self._visible_rows()
        flow = len(rows) if rows else 1
        footer = 0
        if self._keys_hint is not None:
            footer += 1 + self._footer_line_count(self._keys_hint)
        if self._hint_provider is not None:
            footer += 1 + self._footer_line_count(
                self._hint_text(self.selected_value())
            )
        return flow + 2 + footer

    def selected_value(self) -> Optional[str]:
        row = self._selected_row()
        if row is None:
            return None
        return row.value

    def selected_disabled(self) -> bool:
        """Подсвечена видимая недоступная строка: Enter её не берёт. Нет
        видимых строк — не блок: Enter уходит с набранным аргументом."""
        row = self._selected_row()
        return row is not None and not row.enabled

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
        self._rebuild()
