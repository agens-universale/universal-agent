"""Строка итога хода в ленте (P1b, S10; причина ошибки — спека
предохранителя §2.4, ревизия 3).

Уровень 0: глиф в колонке `●`, пустая строка сверху, тусклый цвет. Строит
её `trace_render.build_turn_line` — один билдер для ленты и превью.

Состояния (спека S10 §3.2, §3.4):

* свежая (`-latest`, `freshen`) — строка последнего хода: справа сводка
  правок хода (кликабельна, ведёт на экран изменений в источнике «Ход»);
* осевшая (`settle`) — без сводки; данные сводки и причины строка
  хранит, чтобы освежиться снова;
* за баром (`-held`, `hold` / `reveal`) — скрыта, пока стоит бар
  индикатора.

Причина ошибки — как у строки мысли: за головой ` · ` и тусклый хвост в
одну строку, обрезанный по ширине с `…`. Наведение на обрезанный хвост
красит его `INK`, клик по нему или пробел на выделенной строке
разворачивают причину целиком: под строкой встаёт тело
(`ExpandableTextBody`, внизу `− свернуть`), голова остаётся без хвоста.
Влезающая причина не разворачивается. Свежая строка разворачивается сама,
если причина не влезает; осевшая свёрнута; разворот не хранится.

Строка — один `Static`: в раскладку она встаёт так же быстро, как уходит
бар (S10, «одно движение»). Поэтому тело — соседний виджет ленты сразу за
строкой: строка монтирует его сама, прячет и показывает вместе с собой и
снимает, когда уходит сама.

Кликают только сегмент сводки и обрезанный хвост: рендер запоминает их
x-диапазоны, по цельному `Text` координаты первой клетки не
восстановить."""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.widgets import Static

from gc_tui import textual_compat
from gc_tui.messages import OpenEditsScreenRequest
from gc_tui.palette import DEEP_DIM, DIM, INK
from gc_tui.services.trace_render import fit_lines
from gc_tui.services.turn_outcome import (
    SUMMARY_GAP,
    compose_status_line,
    format_turn_summary,
)
from gc_tui.widgets.click_model import ClickModelMixin, opens_event
from gc_tui.widgets.expandable_text import (
    ExpandableTextBody,
    ExpandableToggle,
)

#: Свежая строка: строка последнего хода, со сводкой.
TURN_LINE_LATEST = "-latest"
#: Строка за баром: скрыта, пока стоит бар индикатора.
TURN_LINE_HELD = "-held"
#: Тело развёрнутой причины — соседний виджет ленты за строкой.
TURN_REASON_CLASS = "turn-reason"
# Ширина, когда измерить нельзя (виджет вне DOM).
_FALLBACK_WIDTH = 80
_FAILED_WORD = "failed"
_REASON_SEP = " · "
# Меньше — у обрезки по словам нет места даже на букву и `…`.
_MIN_TAIL = 2
_EXPANDED = "-expanded"
# Части строки, которые отвечают мыши.
_SUMMARY = "summary"
_TAIL = "tail"


class _ReasonBody(ExpandableTextBody):
    """Развёрнутая причина под строкой: `− свернуть` сворачивает
    строку-владельца."""

    def __init__(self, line: TurnLine, text: str) -> None:
        super().__init__(text, classes=TURN_REASON_CLASS)
        self._line = line

    def on_expandable_toggle(self, message: ExpandableToggle) -> None:
        message.stop()
        self._line.toggle()


class TurnLine(ClickModelMixin, Static):
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
        # След последней отрисовки: по нему мышь отличает клик по сводке
        # и по хвосту причины.
        self._summary_span: tuple[int, int] | None = None
        self._tail_span: tuple[int, int] | None = None
        self._summary_hover = False
        self._tail_hover = False
        # Где НАЧАЛСЯ жест (см. `on_mouse_down`).
        self._pressed_on: str | None = None
        # Разворот причины: свежая строка — сама, дальше — жест. Виден,
        # только если причина не влезла (`_cut`).
        self._open = False
        self._cut = False
        self._body: _ReasonBody | None = None
        # Строка в DOM: `is_mounted` встаёт только после `on_mount`.
        self._placed = False

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
    def is_fresh(self) -> bool:
        return self.has_class(TURN_LINE_LATEST)

    @property
    def expanded(self) -> bool:
        return self.has_class(_EXPANDED)

    def freshen(
        self, summary: tuple[int, int, int] | None, error: str | None
    ) -> None:
        """Строка последнего хода: сводка правок справа, причина
        развёрнута, если не влезает. Повтор с теми же данными ничего не
        меняет — и свёрнутую жестом причину не разворачивает."""
        if self.is_fresh and (summary, error) == (self._summary, self._error):
            return
        if not self.is_fresh or error != self._error:
            self._open = True
            if self._body is not None:
                self._body.set_text((error or "").strip())
        self._summary = summary
        self._error = error
        self.add_class(TURN_LINE_LATEST)
        self._update()

    def settle(self) -> None:
        """Начался следующий ход: без сводки, причина свёрнута; данные
        хранятся."""
        self.remove_class(TURN_LINE_LATEST)
        self._pressed_on = None
        self._summary_hover = False
        self._tail_hover = False
        self._open = False
        self._update()

    def toggle(self) -> None:
        """Развернуть / свернуть причину под строкой (влезающая — нет)."""
        self._open = not self.expanded
        self._update()

    def hold(self) -> None:
        self.add_class(TURN_LINE_HELD)
        self._sync_body()

    def reveal(self) -> None:
        self.remove_class(TURN_LINE_HELD)
        self._sync_body()

    # ---- разворот и тело -------------------------------------------------

    def _reason(self) -> str | None:
        """Причина — только у строки ошибки."""
        return self._error if self._word == _FAILED_WORD else None

    def _fresh_summary(self) -> str | None:
        if self.is_fresh and self._summary:
            return format_turn_summary(*self._summary)
        return None

    def _update(self) -> None:
        """Обрезана ли причина, развёрнута ли строка, тело — под текущую
        ширину; до монтирования это посчитает `on_mount`."""
        if self._placed:
            reason = self._reason()
            self._cut = self._fit_reason(reason, self._fresh_summary())[1]
            # Выделяется только строка с причиной: пробел — её жест.
            self.can_focus = bool(reason)
            self.set_class(self._cut and self._open, _EXPANDED)
            self._sync_body()
        self.refresh(layout=True)

    def _sync_body(self) -> None:
        """Тело — сразу за строкой; видно, пока строка развёрнута и не за
        баром. Монтируется при первом развороте, в том числе за баром:
        строка и тело встают одним движением."""
        if self._body is None and self.expanded and self.parent is not None:
            self._body = _ReasonBody(self, (self._error or "").strip())
            self.parent.mount(self._body, after=self)
        if self._body is not None:
            held = self.has_class(TURN_LINE_HELD)
            self._body.display = self.expanded and not held

    def on_mount(self) -> None:
        self._placed = True
        self._update()

    def on_unmount(self) -> None:
        """Строка ушла (откат, замена записью, перерисовка) — и тело."""
        body = self._body
        if body is not None and not textual_compat.is_being_removed(body):
            body.remove()

    # ---- отрисовка -------------------------------------------------------

    def render(self) -> Text:
        self._summary_span = None
        self._tail_span = None
        summary = self._fresh_summary()
        tail, cut = self._fit_reason(self._reason(), summary)
        head = self.text.copy()
        if tail and not self.expanded:
            head.append(_REASON_SEP)
            start = head.cell_len
            head.append(tail, style=INK if cut and self._tail_hover else DIM)
            if cut:
                self._tail_span = (start, head.cell_len)
        if not self.is_fresh:
            return head
        # Голова — тем же `DEEP_DIM`, что у осевшей (цвет из стиля):
        # явным стилем она не сливается в один прогон с зазором до сводки.
        head.stylize(DEEP_DIM, 0, len(self.text))
        line, self._summary_span = compose_status_line(
            head, summary, self._status_width(), hover=self._summary_hover
        )
        return line

    def _fit_reason(
        self, reason: str | None, summary: str | None
    ) -> tuple[str, bool]:
        """`(хвост, обрезан ли)`: причина одной строкой в место между
        головой и сводкой (сводка, если влезает рядом с головой, места не
        уступает)."""
        if not reason:
            return "", False
        width = self._status_width()
        _, span = compose_status_line(self.text, summary, width)
        reserved = 0 if span is None else span[1] - span[0] + SUMMARY_GAP
        room = width - self.text.cell_len - cell_len(_REASON_SEP) - reserved
        flat = " ".join(reason.split())
        if cell_len(flat) <= room:
            return flat, False
        # Места почти нет: хвост — один `…` или ничего, строка остаётся
        # одной; причина всё равно разворачивается.
        if room < _MIN_TAIL:
            return ("…" if room > 0 else ""), True
        return fit_lines(flat, room, 1), True

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

    def on_resize(self, _event: events.Resize) -> None:
        """Ширина изменилась: сводка и обрезка причины считаются заново."""
        self._update()

    # ---- мышь: сегмент сводки и обрезанный хвост -------------------------

    def _part_at(self, event: events.MouseEvent) -> str | None:
        """Часть строки под указателем — по экранной координате и области
        контента (диапазоны живут в координатах содержимого)."""
        if event.screen_y != self.content_region.y:
            return None
        x = event.screen_x - self.content_region.x
        parts = ((_SUMMARY, self._summary_span), (_TAIL, self._tail_span))
        for part, span in parts:
            if span is not None and span[0] <= x < span[1]:
                return part
        return None

    def _set_hover(self, part: str | None) -> None:
        hover = (part == _SUMMARY, part == _TAIL)
        if hover == (self._summary_hover, self._tail_hover):
            return
        self._summary_hover, self._tail_hover = hover
        self.refresh()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """Запомнить, где жест НАЧАЛСЯ: протяжка выделения от головы,
        отпущенная на сводке, даёт `Click` там же, где настоящее нажатие,
        — отличить их можно только по началу жеста."""
        plain = event.button == 1 and not opens_event(event)
        self._pressed_on = self._part_at(event) if plain else None

    def on_click(self, event: events.Click) -> None:
        """Клик по сводке — экран изменений в источнике «Ход»; по
        обрезанному хвосту — развернуть причину.

        Засчитывается один короткий жест левой кнопкой, начатый и
        отпущенный на той же части: двойной клик (`chain != 1`) открывал
        экран дважды, у правой кнопки своей семантики нет, протяжка
        выделения — жест про текст. Остальное — клик-модели."""
        pressed, self._pressed_on = self._pressed_on, None
        if event.button != 1 or event.chain != 1:
            return
        part = self._part_at(event)
        if pressed is None or part != pressed:
            return
        event.stop()
        event.prevent_default()
        if part == _TAIL:
            self.toggle()
            return
        # Локально: `edits_view` тянет слой `agent_edits`, а строка живёт в
        # каждом кадре ленты.
        from gc_tui.services.edits_view import SOURCE_TURN

        self.post_message(OpenEditsScreenRequest(SOURCE_TURN))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self._set_hover(self._part_at(event))

    def on_leave(self, _event: events.Leave) -> None:
        # Начатый на строке жест закончится уже не здесь.
        self._pressed_on = None
        self._set_hover(None)

    # ---- клавиатура ------------------------------------------------------

    def _open_request(self) -> None:
        """Экрана события у строки итога нет."""
        return None

    def on_key(self, event: events.Key) -> None:
        """Пробел — развернуть / свернуть причину; остальное — клик-модели
        (Textual зовёт её `on_key` следом по MRO)."""
        if event.key == "space" and (self._cut or self.expanded):
            event.stop()
            event.prevent_default()
            self.toggle()
