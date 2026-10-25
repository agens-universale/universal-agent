"""Лента у низа и накладки над ней — общее экранов с лентой `#trace`.

Липкость к низу держит якорь Textual (`textual_compat.AnchoredScroll`):
флаг `_stick_bottom` и якорь меняются в одной точке, выделение якорь
снимает, его конец — возвращает после детекта ухода вверх. «Не внизу» —
накладки (спека P3 §2.6): липкая реплика сверху и плашка «в конец»; клик
по плашке — к низу, по реплике — к ней. Прокрутка при раскрытии и свёртке
блока (спека P4 §9.3) — `_toggle_trace_block`. Экран ставит миксин перед
`Screen`, монтирует `#trace` как `AnchoredScroll`, накладки —
`trace_overlays()`, и после монтирования зовёт `_watch_trace_follow()`.
Подпись и ряд плашки экран может переопределить."""

from __future__ import annotations

from typing import Optional

from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widget import Widget

from gc_tui import textual_compat
from gc_tui.messages import BlockToggle, ScrollToEnd, ScrollToReply
from gc_tui.services.trace_scroll import reply_above
from gc_tui.widgets.trace_overlays import (
    ScrollEndPill,
    StickyReply,
    pill_label,
)

_SEL_TRACE = "#trace"
_SEL_STICKY = "#trace-sticky-reply"
_SEL_PILL = "#trace-end-pill"


def trace_overlays(trace: Widget) -> tuple[StickyReply, ScrollEndPill]:
    """Накладки над лентой `trace`: липкая реплика и плашка «в конец»."""
    return (
        StickyReply(trace, id=_SEL_STICKY[1:]),
        ScrollEndPill(trace, id=_SEL_PILL[1:]),
    )


class TraceFollowMixin:
    """Лента у низа, детект ухода вверх и накладки «не внизу»."""

    _STICK_TOL = 2

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._stick = True  # намерение «липнуть к низу» (_stick_bottom)
        self._last_auto_y = 0  # куда в прошлый раз программно прижали ленту
        # Кэш реплик ленты для липкой реплики (_trace_replies).
        self._trace_replies_key: Optional[tuple] = None
        self._trace_replies_cache: list = []

    def _watch_trace_follow(self) -> None:
        """Отклик на прокрутку ленты и на выделение — по событию."""
        self.watch(
            self.query_one(_SEL_TRACE, VerticalScroll),
            "scroll_y",
            self._on_trace_scrolled,
            init=False,
        )
        textual_compat.watch_selecting(self, self._on_select_freeze)

    def _ui_frozen_for_select(self) -> bool:
        """F1: заморожен ли UI из-за app-select. Сигнал — Screen._selecting."""
        return textual_compat.is_selecting(self)

    def _trace_is_at_bottom(self) -> bool:
        """Сырой замер: трейс прокручен в самый низ (с допуском).
        Используется в _update_stick_state для повторного «прилипания»,
        когда юзер вернулся к низу."""
        try:
            trace = self.query_one(_SEL_TRACE, VerticalScroll)
        except Exception:
            return True
        return trace.scroll_offset.y >= trace.max_scroll_y - self._STICK_TOL

    def _update_stick_state(self) -> None:
        """Поддержать намерение «липнуть к низу» (_stick_bottom). Сравниваем
        offset с местом, куда сами в прошлый раз прижали (_last_auto_y), а
        НЕ с max_scroll_y: контент растёт вниз, offset при этом не
        двигается, поэтому добавление контента липкость не роняет — её
        снимает только реальный уход юзера вверх. Возврат к низу —
        возвращает. Вызывать ПЕРЕД прижатием (детект всегда предшествует
        скроллу → юзера не «дёргает» назад при уходе вверх). Offset ниже
        прижатого, но лента у низа — это не уход вверх: выросшая лента
        (снят баннер, сжался или закрыт список над вводом) сама срезала
        прокрутку до нового низа."""
        try:
            trace = self.query_one(_SEL_TRACE, VerticalScroll)
        except Exception:
            return
        if self._stick_bottom:
            if (
                trace.scroll_offset.y < self._last_auto_y - self._STICK_TOL
                and not self._trace_is_at_bottom()
            ):
                self._stick_bottom = False
        elif self._trace_is_at_bottom():
            self._stick_bottom = True

    def _on_trace_scrolled(self, _value: float) -> None:
        """Отклик на прокрутку ленты по событию, не по опросу. Наш
        scroll_end липкость не снимает (offset не меньше _last_auto_y). Во
        время выделения (F2) — ни детекта, ни прижатия."""
        if self._ui_frozen_for_select():
            return
        self._update_stick_state()
        self._refresh_trace_overlays()

    def _refresh_trace_overlays(self) -> None:
        """Накладки над лентой (спека P3 §2.6): «не внизу» — липкая реплика
        (ближайшая выше края) и плашка «в конец»; внизу — обе скрыты.
        Зовётся на каждый шаг прокрутки: реплики ищутся по кэшу верхних
        реплик (`_trace_replies`), без обхода ленты."""
        try:
            trace = self.query_one(_SEL_TRACE, VerticalScroll)
            sticky = self.query_one(_SEL_STICKY, StickyReply)
            pill = self.query_one(_SEL_PILL, ScrollEndPill)
        except NoMatches:
            return
        # Коробка накладок — ширина подложки реплики в ленте: без колонки
        # скроллбара (решение 27). Плашка центруется по ней же.
        box = trace.scrollable_content_region
        if self._stick_bottom:
            sticky.show(None, box)
            pill.show(None, box)
            return
        replies = self._trace_replies(trace)
        reply = reply_above(
            replies, round(trace.scroll_y), lambda r: r.virtual_region.y
        )
        sticky.show(reply, box)
        pill.show(self._end_pill_label(), box, self._pill_row(trace))

    def _pill_row(self, trace: VerticalScroll) -> int:
        """Ряд плашки «в конец» — последний ряд ленты."""
        return trace.region.bottom - 1

    def _end_pill_label(self) -> str:
        """Подпись плашки: без счётчика новых реплик и без Enter."""
        return pill_label(0, enter=False)

    def _trace_replies(self, trace: VerticalScroll) -> list:
        """Реплики верхнего уровня ленты по порядку. Кэшируется только
        состав; верх каждой читается живым при поиске, поэтому сдвиг
        реплик кэш не портит. Ключ — число детей и первый с последним:
        точный ключ (все дети) стоил бы столько же, сколько пересборка;
        этот ловит и снятие с монтированием в конец за один такт."""
        from gc_tui.widgets.user_reply import UserReplyContainer

        children = trace.children
        key = (len(children), children[0], children[-1]) if children else ()
        if key != self._trace_replies_key:
            self._trace_replies_key = key
            self._trace_replies_cache = [
                child
                for child in trace.children
                if isinstance(child, UserReplyContainer)
            ]
        return self._trace_replies_cache

    def _toggle_trace_block(self, views, message: BlockToggle) -> None:
        """Раскрыть / свернуть блок ленты моделями `views` (§9.3): внизу —
        остаться внизу; иначе после перекладки довести верх блока до
        видимости."""
        at_bottom = self._stick_bottom
        routed = views.route(message)
        if routed is None:
            return
        view, key = routed
        if at_bottom:
            self._scroll_trace_end()
            return
        head = view.head_widget(key)
        if head is not None:
            self.call_after_refresh(head.scroll_visible, animate=False)

    def on_scroll_to_reply(self, message: ScrollToReply) -> None:
        """Клик по липкой реплике — она наверху видимой области."""
        if not message.reply.is_attached:
            return  # реплику успели снять
        trace = self.query_one(_SEL_TRACE, VerticalScroll)
        trace.scroll_to_widget(message.reply, animate=False, top=True)

    def on_scroll_to_end(self, _message: ScrollToEnd) -> None:
        """Клик по плашке «в конец» — то же, что `_scroll_to_bottom`."""
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        """Одно действие «к низу»: клик по плашке и клавиши экрана."""
        self._stick_bottom = True
        self._scroll_trace_end()

    @property
    def _stick_bottom(self) -> bool:
        return self._stick

    @_stick_bottom.setter
    def _stick_bottom(self, stick: bool) -> None:
        """Единственная точка смены липкости: флаг и якорь ленты вместе."""
        self._stick = stick
        self._sync_trace_anchor()

    def _on_select_freeze(self, selecting: bool) -> None:
        """Выделение (F2) снимает якорь; по его концу ушедшую вверх ленту
        к низу не тянем — сначала детект, потом якорь."""
        if not selecting:
            self._update_stick_state()
        self._sync_trace_anchor()

    def _sync_trace_anchor(self) -> None:
        """Якорь Textual держит ленту у низа в том же проходе раскладки,
        что и перемена (рост, свёртка, хром у ввода), — одно движение.
        Стоит, пока лента липнет к низу и нет выделения (F2): выделение
        его снимает, конец выделения — возвращает."""
        trace = self.query_one_optional(_SEL_TRACE, VerticalScroll)
        wanted = self._stick and not self._ui_frozen_for_select()
        if trace is not None and trace.is_anchored is not wanted:
            trace.anchor(wanted)

    def _scroll_trace_end(self) -> None:
        """Прижать трейс к низу: вернуть якорь (его мог снять ручной
        скролл, оставшийся в допуске). Сам низ ставит раскладка, в том же
        проходе; после неё — запомнить позицию для _update_stick_state."""
        if not self._stick_bottom or self._ui_frozen_for_select():
            return
        trace = self.query_one_optional(_SEL_TRACE, VerticalScroll)
        if trace is not None:
            trace.anchor()
            self.call_after_refresh(self._note_auto_y, trace)

    def _note_auto_y(self, trace: VerticalScroll) -> None:
        """Низ после раскладки — пока лента всё ещё липнет к нему."""
        if self._stick_bottom:
            self._last_auto_y = trace.max_scroll_y
