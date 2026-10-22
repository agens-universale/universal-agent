"""Строка итога хода в ленте (P1b, S10).

Уровень 0: глиф в колонке `●`, пустая строка сверху, тусклый цвет. Строит
её `trace_render.build_turn_line` — один билдер для ленты и превью.

Состояния (спека S10 §3.2, §3.4):

* свежая (`-latest`, `freshen`) — строка последнего хода: справа сводка
  правок хода (кликабельна, ведёт на экран изменений в источнике «Ход»),
  под «✕ Ошибка» — причина `└` до трёх строк;
* осевшая (`settle`) — только голова; данные сводки и причины строка
  хранит, чтобы освежиться снова;
* за баром (`-held`, `hold` / `reveal`) — скрыта, пока стоит бар
  индикатора.

Кликает только сам сегмент сводки: рендер запоминает его x-диапазон, по
цельному `Text` координаты первой клетки сегмента не восстановить."""

from __future__ import annotations

import textwrap

from rich.text import Text
from textual import events
from textual.widgets import Static

from gc_tui.messages import OpenEditsScreenRequest
from gc_tui.palette import DEEP_DIM, DIM
from gc_tui.services.trace_render import consequence_lead
from gc_tui.services.turn_outcome import (
    compose_status_line,
    format_turn_summary,
)

#: Свежая строка: строка последнего хода, со сводкой и причиной.
TURN_LINE_LATEST = "-latest"
#: Строка за баром: скрыта, пока стоит бар индикатора.
TURN_LINE_HELD = "-held"
#: Причина ошибки переносится не больше чем на столько строк.
ERROR_MAX_ROWS = 3
# Ширина, когда измерить нельзя (виджет вне DOM).
_FALLBACK_WIDTH = 80
_FAILED_WORD = "failed"


class TurnLine(Static):
    """Строка итога одного хода. `opened_by` — реплика, открывшая ход
    (из записи итога): по нему откат реплики снимает строку. `word` и
    `plan` — исход по словарю `turn_outcome`; `recorded` — строка стоит
    по записи итога, а не по исходу раннера или статусу сессии."""

    def __init__(
        self,
        text: Text,
        *,
        opened_by: str | None,
        word: str,
        plan: bool,
        recorded: bool,
    ) -> None:
        super().__init__(text, markup=False)
        self.text = text
        self.opened_by = opened_by
        self._word = word
        self._plan = plan
        self._recorded = recorded
        self._summary: tuple[int, int, int] | None = None
        self._error: str | None = None
        # След последней отрисовки: по нему мышь отличает клик по сводке.
        self._summary_span: tuple[int, int] | None = None
        self._summary_hover = False
        # Жест начался НА сегменте (см. `on_mouse_down`).
        self._pressed_in_summary = False

    # ---- состояние -------------------------------------------------------

    @property
    def word(self) -> str:
        return self._word

    @property
    def plan(self) -> bool:
        return self._plan

    @property
    def recorded(self) -> bool:
        return self._recorded

    @property
    def summary(self) -> tuple[int, int, int] | None:
        return self._summary

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def summary_span(self) -> tuple[int, int] | None:
        return self._summary_span

    @property
    def is_fresh(self) -> bool:
        return self.has_class(TURN_LINE_LATEST)

    def freshen(
        self, summary: tuple[int, int, int] | None, error: str | None
    ) -> None:
        """Строка последнего хода: сводка правок справа, причина под
        «✕ Ошибка». Повтор с теми же данными ничего не перерисовывает."""
        if self.is_fresh and (summary, error) == (self._summary, self._error):
            return
        self._summary = summary
        self._error = error
        self.add_class(TURN_LINE_LATEST)
        self.refresh(layout=True)

    def settle(self) -> None:
        """Начался следующий ход: остаётся голова, данные хранятся."""
        self.remove_class(TURN_LINE_LATEST)
        self._pressed_in_summary = False
        self._summary_hover = False
        self.refresh(layout=True)

    def hold(self) -> None:
        self.add_class(TURN_LINE_HELD)

    def reveal(self) -> None:
        self.remove_class(TURN_LINE_HELD)

    # ---- отрисовка -------------------------------------------------------

    def render(self) -> Text:
        self._summary_span = None
        if not self.is_fresh:
            return self.text
        summary = (
            format_turn_summary(*self._summary) if self._summary else None
        )
        # Голова — тем же `DEEP_DIM`, что у осевшей (цвет из стиля):
        # явным стилем она не сливается в один прогон с зазором до сводки.
        head = self.text.copy()
        head.stylize(DEEP_DIM)
        line, self._summary_span = compose_status_line(
            head,
            summary,
            self._status_width(),
            hover=self._summary_hover,
        )
        if self._word == _FAILED_WORD and self._error:
            line.append("\n")
            line.append_text(self._error_lines())
        return line

    def _status_width(self) -> int:
        """Своя ширина, иначе экрана, иначе страховка (вне DOM)."""
        for probe in (lambda: self.size.width, lambda: self.app.size.width):
            try:
                width = int(probe())
            except Exception:
                width = 0
            if width > 0:
                return width
        return _FALLBACK_WIDTH

    def _error_texts(self) -> list[str]:
        """Причина по строкам колонки текста (под `└ `): до трёх строк,
        дальше — обрезка `…` в последней."""
        col = max(1, self._status_width() - consequence_lead(0).cell_len)
        rows = textwrap.wrap(
            self._error or "",
            col,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        if len(rows) <= ERROR_MAX_ROWS:
            return rows
        last = rows[ERROR_MAX_ROWS - 1]
        return rows[: ERROR_MAX_ROWS - 1] + [last[: col - 1].rstrip() + "…"]

    def _error_lines(self) -> Text:
        """`└ <причина>` — следствие уровня 0, `DIM`; продолжение — под
        колонкой текста, не под `└`."""
        lead = consequence_lead(0)
        indent = " " * lead.cell_len
        out = Text()
        for i, row in enumerate(self._error_texts()):
            if i:
                out.append("\n" + indent)
            else:
                out.append_text(lead)
            out.append(row, style=DIM)
        return out

    def on_resize(self, _event: events.Resize) -> None:
        """Ширина изменилась: сводка и перенос причины считаются заново."""
        if self.is_fresh:
            self.refresh(layout=True)

    # ---- сегмент сводки: единственная кликабельная часть строки ----------

    def _in_summary(self, event: events.MouseEvent) -> bool:
        """Указатель на сегменте сводки — по экранной координате и области
        контента (диапазон сегмента живёт в координатах содержимого)."""
        span = self._summary_span
        if span is None or event.screen_y != self.content_region.y:
            return False
        x = event.screen_x - self.content_region.x
        return span[0] <= x < span[1]

    def _set_summary_hover(self, hover: bool) -> None:
        if hover == self._summary_hover:
            return
        self._summary_hover = hover
        self.refresh()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """Запомнить, что жест НАЧАЛСЯ на сегменте: протяжка выделения от
        головы, отпущенная на сводке, даёт `Click` там же, где настоящее
        нажатие, — отличить их можно только по началу жеста."""
        self._pressed_in_summary = event.button == 1 and self._in_summary(
            event
        )

    def on_click(self, event: events.Click) -> None:
        """Клик по сводке — экран изменений в источнике «Ход».

        Засчитывается один короткий жест левой кнопкой, начатый и
        отпущенный на сегменте: двойной клик (`chain != 1`) открывал экран
        дважды, у правой кнопки своей семантики нет, протяжка выделения —
        жест про текст."""
        pressed, self._pressed_in_summary = self._pressed_in_summary, False
        if event.button != 1 or event.chain != 1:
            return
        if not pressed or not self._in_summary(event):
            return
        event.stop()
        # Локально: `edits_view` тянет слой `agent_edits`, а строка живёт в
        # каждом кадре ленты.
        from gc_tui.services.edits_view import SOURCE_TURN

        self.post_message(OpenEditsScreenRequest(SOURCE_TURN))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self._set_summary_hover(self._in_summary(event))

    def on_leave(self, _event: events.Leave) -> None:
        # Начатый на сегменте жест закончится уже не здесь.
        self._pressed_in_summary = False
        self._set_summary_hover(False)
