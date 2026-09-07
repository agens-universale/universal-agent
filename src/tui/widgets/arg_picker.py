"""ArgPicker — overlay-список значений аргумента команды (value + описание [+ статус]).

Зеркало `SkillPicker`/`CommandPalette`: те же CSS-классы (.palette / .palette-row /
.palette-hint) и модель состояния (filter + selected index). Опции приходят готовым
`list[tuple[str, str] | tuple[str, str, str]]` (value, description[, status]) из
`Command.arg_options` — открывает вызывающий код (TraceScreen) для любой команды с
непустым arg_options. 2-tuple — простое value+описание (`/export` и т.п.); 3-tuple
добавляет status-колонку справа (`/profiles`, задача 10 — активный профиль,
цветом OK; ✗-статус отказа — цветом ERR)."""
from __future__ import annotations

import textwrap
from typing import Callable, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from tui import palette as _pal


_HINT = "↑↓ выбрать · Tab дополнить · Enter выполнить · Esc закрыть"
_NO_MATCH = "(нет совпадений)"


class ArgPicker(VerticalScroll):
    """Filtered overlay-список значений аргумента."""

    DEFAULT_CSS = ""  # стили — .palette / .palette-row / .palette-hint в styles.tcss

    def __init__(
        self,
        options: "list[tuple[str, str] | tuple[str, str, str]]",
        initial_filter: str = "",
        hint_provider: Optional[Callable[[Optional[str]], str]] = None,
    ) -> None:
        super().__init__(classes="palette")
        self._options = options
        self._filter = initial_filter
        self._selected = 0
        # Динамический хинт подсвеченного пункта (спека §12.1, /profiles: состав
        # слотов профиля) — строкой над статичной клавиш-подсказкой подвала.
        self._hint_provider = hint_provider
        # Кэш последнего посчитанного хинта — на ОДИН выбор (см. `_hint_text`):
        # `_build_footer` и `desired_height` спрашивали провайдер каждый по-своему,
        # а поллинг главного экрана (0.25 с) дёргает `desired_height`, пока пикер
        # просто открыт — без кэша полная проба (у /profiles — сборка конфига
        # целиком) шла 4 раза в секунду на UI-потоке ради текста одного и того же
        # пункта.
        self._hint_cache: Optional[tuple] = None
        self._apply_size()  # честная высота с самого первого layout-прохода (см. _apply_size)

    def compose(self) -> ComposeResult:
        rows = self._visible_rows()
        if not rows:
            yield Static(_NO_MATCH, classes="palette-hint", markup=False)
        else:
            for i, opt in enumerate(rows):
                classes = "palette-row selected" if i == self._selected else "palette-row"
                yield Static(self._format_row(opt), classes=classes, markup=False)
        yield self._build_footer()

    def _build_footer(self) -> Vertical:
        """Докнутый подвал (паттерн CommandPalette._build_footer): hint — ПОСЛЕДНИЙ
        FLOW-ребёнок в старой раскладке упирался в max-height-клэмп длинного списка
        и прятался за скроллом (регрессия воздуха .palette-keys, коммит c0622c9a).
        Докнутый контейнер вне скролла держит hint видимым при открытии всегда."""
        children = []
        if self._hint_provider is not None:
            # palette-keys и у динамического хинта: воздух над ним, отделяющий
            # хинт от строк списка (ревизия 2026-07-28 по живому смоку).
            children.append(Static(self._hint_text(self.selected_value()),
                                   classes="palette-hint palette-keys", markup=False))
        children.append(Static(_HINT, classes="palette-hint palette-keys", markup=False))
        return Vertical(*children, classes="palette-footer")

    def _value_col_w(self) -> int:
        """Ширина value-колонки: value паддируется до самого длинного value
        среди ВИДИМЫХ (отфильтрованных) опций + 3 пробела (то же правило
        выравнивания, что и в CommandPalette, спека 2026-07-18 §4)."""
        rows = self._visible_rows()
        if not rows:
            return 0
        return max(len(opt[0]) for opt in rows) + 3

    def _desc_col_w(self) -> int:
        """Ширина description-колонки для опций СО статусом (3-tuple, `/profiles`,
        задача 10): описание паддится до самого длинного описания среди ВИДИМЫХ
        строк + 3 пробела — тот же принцип, что у `_value_col_w`, чтобы статус
        (правая колонка) стартовал в одном месте у всех строк."""
        descs = [opt[1] for opt in self._visible_rows() if len(opt) == 3]
        if not descs:
            return 0
        return max(len(d) for d in descs) + 3

    def _format_row(self, opt: tuple) -> "str | Text":
        """Строка списка: `(value, desc)` — как раньше, простой `str` (другие
        пикеры — /export и т.п. — не должны заметить правку). `(value, desc,
        status)` — опции со статус-колонкой (`/profiles`, задача 10): `Text` с
        правым статусом цветом OK, ровно как активный маркер в /settings
        (`_profile_row_text`), и цветом ERR у ✗-отказа; статус пуст у строк,
        которым нечего сказать."""
        if len(opt) == 3:
            value, desc, status = opt
            if not desc and not status:
                return Text(f" {value}")
            t = Text(f" {value.ljust(self._value_col_w())}{desc.ljust(self._desc_col_w())}")
            if status:
                # Цвет — по смыслу статуса: ✗-отказ («не применится») зелёным
                # цветом достижения читался бы как «всё в порядке». Правило по
                # глифу, а не по литералу: пикер не знает ни про профили, ни про
                # их тексты.
                t.append(status, style=_pal.ERR if status.startswith("✗") else _pal.OK)
            return t
        value, desc = opt
        if not desc:
            return f" {value}"
        return f" {value.ljust(self._value_col_w())}{desc}"

    def _visible_rows(self) -> "list[tuple[str, str] | tuple[str, str, str]]":
        f = self._filter.strip().lower()
        if not f:
            return list(self._options)
        return [o for o in self._options if o[0].lower().startswith(f)]

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
        пункта — а `desired_height` вдобавок дёргается поллингом главного экрана
        (0.25 с) на каждом тике, пока пикер просто открыт. Кэш на одну запись
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
        main.py::_sync_trace_max_height берёт это значение напрямую — недосчёт
        выдавливал бы нижнюю рамку пикера за экран."""
        rows = self._visible_rows()
        flow = len(rows) if rows else 1
        footer = 1 + self._footer_line_count(_HINT)
        if self._hint_provider is not None:
            footer += 1 + self._footer_line_count(self._hint_text(self.selected_value()))
        return flow + 2 + footer

    def set_filter(self, prefix: str) -> None:
        self._filter = prefix
        self._selected = 0
        self._rebuild()

    def move_selection(self, delta: int) -> None:
        rows = self._visible_rows()
        if not rows:
            return
        self._selected = (self._selected + delta) % len(rows)
        self._rebuild()

    def selected_value(self) -> Optional[str]:
        rows = self._visible_rows()
        if not rows:
            return None
        idx = max(0, min(self._selected, len(rows) - 1))
        return rows[idx][0]

    def _rebuild(self) -> None:
        if not self.is_mounted:
            return
        rows = self._visible_rows()
        new_widgets: list = []
        selected_widget: Optional[Static] = None
        if not rows:
            new_widgets.append(Static(_NO_MATCH, classes="palette-hint", markup=False))
        else:
            for i, opt in enumerate(rows):
                classes = "palette-row selected" if i == self._selected else "palette-row"
                w = Static(self._format_row(opt), classes=classes, markup=False)
                if i == self._selected:
                    selected_widget = w
                new_widgets.append(w)
        new_widgets.append(self._build_footer())
        self._apply_size()
        self.remove_children()
        self.mount_all(new_widgets)
        if selected_widget is not None:
            self.call_after_refresh(selected_widget.scroll_visible, animate=False)
