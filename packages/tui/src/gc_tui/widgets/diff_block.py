"""DiffBlock — дифф одной правки агента прямо в ленте трейса.

Устройство:
    DiffBlock.diff-block
        Static.block-body        — rich.Text движка render_hunks, на подложке
        Static.diff-more         — «… ещё N строк» / «свернуть», на подложке

Блок — следствие своей строки инструмента: стоит сразу под ней, на +2 от её
глифа (правила `.diff-block` в styles.tcss), тело — с колонки блока. Счётчики
`+N −M` и кнопка ❐ живут в самой строке вызова (`ToolCallRow.set_edit_counts`,
спека P3 §10): имя файла там уже есть, второй строки-ветки блоку не нужно.

Тело монтируется, только если в свёрнутом состоянии есть что показать: у
компактного блока (`compact=True` — старый ход при реплее) его нет вовсе,
остаётся хвост-разворот; у записи без диффа (бинарь/удаление) нет и хвоста —
блок пуст, счётчики только в строке вызова.

Хвост невидим для выделения (`NoSelectStatic`: ALLOW_SELECT=False +
get_selection()->None) — иначе в буфер попадала бы его подпись.

Чем отличается от BlockPanel, устройство которого повторяет:

* тело считает движок диффов, и в свёрнутом виде — с `max_lines`. Это не
  косметика: замер Task 2 на диффе в 2000 строк даёт 33 мс с лимитом против
  114 мс без него. Оба состояния кэшируются по (hunks, path, max_lines,
  fill_width) в пределах виджета — своего кэша у движка нет, а тогл и ресайзы
  иначе пересчитывали бы одно и то же;
* от ширины зависит фоновая заливка строк (она тянется до края подложки), но не
  раскладка: переноса нет. Поэтому тело пересобирается на resize — тем же
  приёмом, что таблица в BlockPanel, — но только когда ширина реально сменилась;
* в буфер уходят сырые хэнки записи, а не `.plain` отрендеренного текста — в
  нём номера строк, маркеры и хвостовые пробелы фоновой заливки.
"""

from __future__ import annotations

from typing import Optional

from gc_core.agent_edits.journal import EditRecord
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from gc_tui.services.diff_render import (
    DiffRender,
    copyable_hunks,
    render_hunks,
)
from gc_tui.services.plural import lines_word
from gc_tui.widgets.block_panel import CopyFeedbackMixin, _copy_request
from gc_tui.widgets.click_model import ClickModelMixin
from gc_tui.widgets.no_select import NoSelectStatic

COLLAPSE_LABEL = "свернуть"


class DiffBlock(CopyFeedbackMixin, ClickModelMixin, Vertical):
    """Блок правки: свёрнутое тело диффа и хвост-тогл."""

    ALLOW_SELECT = False  # выделяется тело, а не блок целиком
    # Блок НЕ участвует в Tab-обходе и вообще не принимает фокус (живая правка
    # 2026-08-11). Событие трейса — это ВЫЗОВ ИНСТРУМЕНТА, а дифф лишь его часть:
    # Tab обязан идти по событиям, останавливаясь на строке тула, а следующий Tab
    # — на следующем событии. Отдельный стоп на диффе ломал этот ритм.
    # Следствие: Enter-тогла свёртки нет, разворачивает только клик по хвосту.
    # FOCUS_ON_CLICK = False — заявка «нажатие по мне фокус не двигает». Сам
    # Textual её не увидит: на MouseDown он ищет фокусируемого ПРЕДКА нажатого
    # виджета и спрашивает флаг только у найденного, а нефокусируемый блок
    # обходом не останавливается — найденным оказывался контейнер #trace, и
    # клик по диффу сбивал позицию Tab. Заявку предъявляет экран трейса:
    # `TraceScreen.get_focusable_widget_at` → `click_model.focus_target_at`.
    FOCUS_ON_CLICK = False

    # Отступ следствия задаёт сам блок (`.diff-block` в styles.tcss); тело и
    # хвост — с его колонки.
    DEFAULT_CSS = """
    DiffBlock { height: auto; width: 1fr; }
    DiffBlock .block-body {
        width: 1fr; height: auto; text-wrap: nowrap; text-overflow: clip;
    }
    DiffBlock .diff-more { width: 1fr; height: 1; }
    """

    def __init__(
        self,
        record: EditRecord,
        *,
        event_no: int,
        collapsed_max_lines: int,
        compact: bool = False,
    ) -> None:
        super().__init__(classes="diff-block")
        self._record = record
        self._event_no = event_no
        # compact — стартовое состояние блока старого хода при реплее: тела нет
        # совсем, только шапка и хвост-разворот. Выражено нулевым порогом
        # свёртки: движок при max_lines=0 отдаёт пустое тело и честный total,
        # так что и хвост «… ещё N строк», и разворот работают как обычно.
        self._collapsed_max_lines = 0 if compact else collapsed_max_lines
        self._expanded = False
        self._cache: dict[
            tuple[str, str, Optional[int], Optional[int]], DiffRender
        ] = {}
        self._fill: Optional[int] = None  # ширина заливки текущего тела
        self._restore_timer = None
        self.can_focus = (
            False  # см. FOCUS_ON_CLICK выше: дифф — не событие трейса
        )

    @classmethod
    def for_record(
        cls,
        record: EditRecord,
        *,
        event_no: int,
        collapsed_max_lines: int,
        compact: bool = False,
    ) -> DiffBlock:
        """Блок по записи журнала правок."""
        return cls(
            record,
            event_no=event_no,
            collapsed_max_lines=collapsed_max_lines,
            compact=compact,
        )

    # --- сборка ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        collapsed = self._diff_render(expanded=False)
        if collapsed.total_lines == 0:
            return  # binary / delete / no_diff — пустой блок, без рамки
        if collapsed.shown_lines:
            yield Static(collapsed.text, classes="block-body", markup=False)
        if collapsed.truncated:
            yield NoSelectStatic(
                self._tail_label(),
                classes="diff-more trace-action",
                markup=False,
            )

    def on_mount(self) -> None:
        self._sync_body()

    def on_resize(self, event: events.Resize) -> None:
        """Ширина сменилась — заливка строк тянется до нового края подложки.

        Сверка ширины обязательна: Resize приходит и на смену высоты, а полный
        рендер диффа в 2000 строк стоит 114 мс."""
        if self._fill_width() != self._fill:
            self._sync_body()

    def _fill_width(self) -> Optional[int]:
        """Ширина заливки строки: подложка блока целиком, без полей.

        Полоса правки — ровный прямоугольник: слева она начинается с колонки
        номеров, значит и справа обязана доходить до края. Поле в один символ
        было только справа, и односторонний отступ читался не как воздух, а как
        недорисованная полоса.

        None — ширина ещё не известна (compose до раскладки): тогда движок
        заливает по содержимому, а первый же on_mount/on_resize пересоберёт."""
        width = self.size.width
        return width if width > 0 else None

    def _diff_render(self, *, expanded: bool) -> DiffRender:
        """Дифф нужного состояния; повтор состояния берётся из кэша виджета."""
        max_lines = None if expanded else self._collapsed_max_lines
        fill = self._fill_width()
        key = (self._record.hunks, self._record.path, max_lines, fill)
        cached = self._cache.get(key)
        if cached is None:
            # Ширина — часть ключа: тот же дифф на другой ширине даёт другую
            # заливку. Записи прошлых ширин выбрасываем — иначе протяжка рамки
            # накопила бы по полному рендеру на каждый её шаг.
            self._cache = {
                k: v for k, v in self._cache.items() if k[3] == fill
            }
            cached = render_hunks(
                self._record.hunks,
                path=self._record.path,
                max_lines=max_lines,
                fill_width=fill,
                wrap=True,
            )
            self._cache[key] = cached
        return cached

    def _sync_body(self) -> None:
        """Пересобрать тело под текущее состояние свёртки и ширину блока.

        Тела может не быть в DOM (компактный блок, свёрнутый в ноль строк) —
        тогда оно монтируется перед хвостом; и наоборот, свёртка компактного
        блока тело снимает."""
        self._fill = self._fill_width()
        render = self._diff_render(expanded=self._expanded)
        body = self.query(".block-body")
        if body and render.shown_lines:
            body.first(Static).update(render.text)
        elif body:
            body.first(Static).remove()
        elif render.shown_lines:
            self.mount(
                Static(render.text, classes="block-body", markup=False),
                before=self.query_one(".diff-more"),
            )
        self._sync_tail()

    def _sync_tail(self) -> None:
        """Хвост «… ещё N строк» — под текущую ширину.

        Бюджет свёрнутого блока считается в ЭКРАННЫХ рядах, поэтому и сам факт
        усечения, и число скрытых строк зависят от ширины окна. Хвост, собранный
        один раз в `compose` (до раскладки, по ширине «неизвестно»), протухал бы
        при первом же изменении размера; а если усечение появлялось только после
        раскладки, хвоста не было вовсе — и блок нельзя было развернуть.
        """
        tail = self.query(".diff-more")
        # Порядок в `or` существенный: развёрнутому блоку хвост нужен ВСЕГДА
        # (это ярлык свёртки), а свёрнутый рендер для него — второй полный
        # проход на каждое изменение ширины: у кэша `max_lines` входит в ключ,
        # так что промах гарантирован.
        if self._expanded or self._diff_render(expanded=False).truncated:
            if tail:
                tail.first(Static).update(self._tail_label())
            else:
                self.mount(
                    NoSelectStatic(
                        self._tail_label(),
                        classes="diff-more trace-action",
                        markup=False,
                    )
                )
        elif tail:
            tail.first(Static).remove()

    def _tail_label(self) -> str:
        if self._expanded:
            return COLLAPSE_LABEL
        collapsed = self._diff_render(expanded=False)
        # Скрытое = целые строки, не влезшие в бюджет, ПЛЮС ряды, срезанные
        # внутри длинной строки: бюджет свёрнутого блока считается в экранных
        # рядах, и перенесённый остаток — такая же скрытая строка экрана.
        remaining = (
            collapsed.total_lines
            - collapsed.shown_lines
            + collapsed.hidden_rows
        )
        return f"… ещё {remaining} {lines_word(remaining)}"

    # --- взаимодействие ----------------------------------------------------

    def on_click(self, event: events.Click) -> None:
        """Хвост переключает свёртку, двойной клик копирует дифф.

        Ctrl+клик пропускает ветку хвоста: спека §2 обещает открытие события с
        любой точки блока.

        `prevent_default()` на входе — по той же причине, что и в on_key:
        Textual вызывает ВСЕ одноимённые хэндлеры по MRO, и без него
        ClickModelMixin.on_click отработал бы вторым — выделял бы блок за клик
        по хвосту и постил бы ВТОРОЙ запрос открытия по Ctrl+клику.
        Миксин зовём сами, явным super() там, где он нужен.
        """
        event.prevent_default()
        widget = getattr(event, "widget", None)
        if (
            not event.ctrl
            and widget is not None
            and widget.has_class("diff-more")
        ):
            # Ветка ПОГЛОЩАЕТ клик по хвосту при любом chain, а переключает
            # только по первому: двойной клик приходит дважды (chain 1, затем
            # 2), и второй раз переключил бы состояние обратно. Фильтр по
            # chain на входе в ветку не годится — тогда второй клик проваливался
            # бы в ветку копирования ниже (она смотрит только на payload
            # записи, не на цель клика) и молча клал в буфер весь дифф.
            event.stop()
            if event.chain == 1:
                self._toggle()
            return
        if event.chain == 2 and self._record.hunks:
            event.stop()
            body = self.query(".block-body")
            if body:
                body.first(Static).text_select_all()
            self._copy_block()
            return
        if event.ctrl:
            # Ctrl+клик — жест «открыть событие», он и выделяет: после выхода с
            # открытого экрана фокус должен остаться здесь, а не на прошлой строке.
            super().on_click(event)
            return
        # Обычный клик мышью блок НЕ выделяет (живая правка 2026-08-11): дифф
        # читают, а не «выбирают», и светло-серая подсветка на пол-экрана мешала
        # читать сам код. Клик остаётся обычным кликом, поэтому протащить курсор
        # и скопировать кусок диффа по-прежнему можно.
        # `super().on_click(event)` не зовём: именно он и ставил выделение.
        event.stop()

    def _toggle(self) -> None:
        """Переключить свёртку; у компактного блока тело появляется/исчезает."""
        self._expanded = not self._expanded
        self._sync_body()  # хвост обновляет `_sync_tail` внутри

    def _copy_block(self) -> None:
        # Отклика ✓ здесь нет: кнопка ❐ — в строке вызова, у неё свой.
        self.post_message(
            _copy_request(
                copyable_hunks(self._record.hunks, path=self._record.path)
            )
        )
