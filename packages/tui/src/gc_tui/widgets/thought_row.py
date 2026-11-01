"""ThoughtRow — строка мысли модели в ленте (спека мыслей §2).

`● Thinking  <хвост> · <таймер>`, пока мысль идёт (глиф мигает `DIM` ↔
пустая клетка ритмом идущего вызова), `● Thought …` после — всё серое.
Части строки под ширину строит `trace_render.thought_line`: таймер не
режется; у идущей — хвост с `…` в начале, у законченной — последнее
предложение с начала (`…` в начале, если до него был текст, и в конце,
если не влезло); пересчёт — при смене ширины, текста
или раскрытия. Обрезанный текст несёт класс `trace-expandable` (ховер
`$ink`): клик по нему или пробел на выделенной строке раскрывают всю
мысль под строкой (`ExpandableTextBody`, внизу `− свернуть`), сама
строка остаётся без текста — `● Thought · 4с`.
Влезающий текст не раскрывается. Обычный клик строку не выделяет (канон
клик-модели); Tab выделяет, Enter или клик с Ctrl/Alt открывают экран
события — у записанной строки (`event_no`); у временной (`event_no=None`)
открывать нечего.
"""

from __future__ import annotations

from typing import Optional

from gc_core.persistence.reasoning_record import ReasoningRecord
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static

from gc_tui.messages import OpenEventRequest
from gc_tui.palette import DIM
from gc_tui.services.trace_glyphs import blink_on
from gc_tui.services.trace_render import THOUGHT_GLYPH, thought_line
from gc_tui.widgets.blink_pulse import BlinkPulseMixin
from gc_tui.widgets.click_model import ClickModelMixin, opens_event
from gc_tui.widgets.expandable_text import (
    ExpandableTextBody,
    ExpandableToggle,
)
from gc_tui.widgets.static_content import update_if_changed

# Пустая клетка справа у полной строки: таймер не прижат к полосе
# прокрутки (как у строки вызова).
_RIGHT_GUTTER = 1
_EXPANDABLE = "trace-expandable"


class _ThoughtText(Static):
    """Хвост мысли: клик по обрезанному — раскрыть / свернуть (клик с
    модификатором уходит строке — открыть событие)."""

    def on_click(self, event: events.Click) -> None:
        if opens_event(event) or not self.has_class(_EXPANDABLE):
            return
        event.stop()
        self.post_message(ExpandableToggle())


class ThoughtRow(BlinkPulseMixin, ClickModelMixin, Widget):
    """Строка мысли: идущая (`Thinking`, мигает) или законченная
    (`Thought`); временная — из стрима, записанная — из записи треда
    (`persist`)."""

    DEFAULT_CSS = """
    ThoughtRow {
        height: auto;
        width: 1fr;
    }
    ThoughtRow > .thought-line { height: auto; width: 1fr; }
    ThoughtRow .thought-line > Static { width: auto; height: 1; }
    ThoughtRow > ExpandableTextBody { padding: 0 0 0 2; display: none; }
    ThoughtRow.-expanded > ExpandableTextBody { display: block; }
    """
    can_focus = True

    def __init__(
        self,
        *,
        text: str,
        live: bool,
        attempt: str | None,
        seconds: float | None,
        event_no: int | None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._text = text
        self._live = live
        self._attempt = attempt
        self._seconds = seconds
        #: Номер события записанной строки (Enter); временная — None.
        self.event_no = event_no
        self._persisted = False
        self._cut = False
        self._pulse_frame = 0
        self._pulse_timer = None
        self._glyph = Static(self._render_glyph(), classes="trace-prefix")
        self._lead = Static("", classes="thought-word")
        self._tail = _ThoughtText("", classes="thought-text", markup=False)
        self._timer = Static("", classes="thought-timer")
        self._body = ExpandableTextBody(text.strip())

    def compose(self) -> ComposeResult:
        with Horizontal(classes="thought-line"):
            yield self._glyph
            yield self._lead
            yield self._tail
            yield self._timer
        yield self._body

    def on_mount(self) -> None:
        self._sync_pulse()
        self._update()

    def on_resize(self, _event: events.Resize) -> None:
        self._update()

    # --- состояние ---------------------------------------------------------

    @property
    def attempt(self) -> str | None:
        return self._attempt

    @property
    def persisted(self) -> bool:
        return self._persisted

    @property
    def expanded(self) -> bool:
        return self.has_class("-expanded")

    def update_live(self, text: str, seconds: float, live: bool) -> None:
        """Мысль из стрима: текст дорастает, таймер идёт; ``live=False``
        — размышление кончилось (`Thought`, мигание стоп)."""
        self._set(text, seconds, live)

    def persist(self, record: ReasoningRecord) -> None:
        """Строка становится записанной: текст и время — из записи."""
        self._persisted = True
        self._set(record.text, record.duration_sec, False)

    def toggle(self) -> None:
        """Раскрыть / свернуть мысль целиком под строкой."""
        self.toggle_class("-expanded")
        self._update()

    def _set(self, text: str, seconds: float | None, live: bool) -> None:
        self._text = text
        self._seconds = seconds
        self._live = live
        self._body.set_text(text.strip())
        self._sync_pulse()
        self._update()

    # --- клик-модель -------------------------------------------------------

    def _open_request(self) -> Optional[OpenEventRequest]:
        if self.event_no is None:
            return None
        return OpenEventRequest(self.event_no)

    def on_key(self, event: events.Key) -> None:
        """Пробел — раскрыть / свернуть; остальное (Enter) — клик-модели:
        Textual зовёт её `on_key` следом по MRO, если не `prevent_default`."""
        if event.key == "space" and (self._cut or self.expanded):
            event.stop()
            event.prevent_default()
            self.toggle()

    def on_expandable_toggle(self, message: ExpandableToggle) -> None:
        message.stop()
        self.toggle()

    # --- вид ---------------------------------------------------------------

    @property
    def _blinking(self) -> bool:
        return self._live

    def _redraw_pulse(self) -> None:
        self._glyph.update(self._render_glyph(), layout=False)

    def _render_glyph(self) -> Text:
        """Глиф `●` `DIM`; идущая мысль мигает исчезая — пустая клетка
        той же ширины."""
        glyph, space = THOUGHT_GLYPH
        if self._live and not blink_on(self._pulse_frame):
            glyph = " "
        return Text().append(glyph, style=DIM).append(space)

    def _update(self) -> None:
        """Строка под ширину; до монтирования — ничего (соберёт
        `on_mount`)."""
        if not self.is_mounted:
            return
        parts = thought_line(
            live=self._live,
            text=self._text,
            seconds=self._seconds,
            width=self._row_width() - _RIGHT_GUTTER,
            expanded=self.expanded,
        )
        self._cut = parts.cut
        self._tail.set_class(parts.cut, _EXPANDABLE)
        update_if_changed(self._glyph, self._render_glyph())
        update_if_changed(self._lead, Text().append(parts.lead, style=DIM))
        update_if_changed(self._tail, parts.tail)
        update_if_changed(self._timer, Text().append(parts.timer, style=DIM))

    def _row_width(self) -> int:
        """Ширина строки; до раскладки — ширина ленты (строка в ней во всю
        ширину)."""
        if self.size.width > 0:
            return self.size.width
        parent = self.parent
        if isinstance(parent, Widget):
            return parent.scrollable_content_region.width
        return 0
