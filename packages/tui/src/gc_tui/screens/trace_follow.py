"""Лента у низа и накладки над ней — общее экранов с лентой `#trace`.

Липкость к низу держит якорь Textual (`textual_compat.AnchoredScroll`):
флаг `_stick_bottom` и якорь меняются в одной точке, выделение якорь
снимает, его конец — возвращает после детекта ухода вверх. «Не внизу» —
накладки (спека P3 §2.6): липкая реплика сверху и плашка «в конец»; клик
по плашке — к низу, по реплике — к ней. Прокрутка при раскрытии и свёртке
блока (спека P4 §9.3) — `_toggle_trace_block`. Экран ставит миксин перед
`Screen`, монтирует `#trace` как `AnchoredScroll`, накладки —
`trace_overlays()`, и после монтирования зовёт `_watch_trace_follow()`.
Подпись и ряд плашки экран может переопределить. Виджеты, пропустившие
кадр за экраном, экран ведёт слабым множеством и после раскладки
предлагает им освежиться (П6): вернувшийся на экран сразу показывает
текущее состояние."""

from __future__ import annotations

import weakref

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
from gc_tui.widgets.user_reply import UserReplyContainer

_SEL_TRACE = "#trace"
_SEL_STICKY = "#trace-sticky-reply"
_SEL_PILL = "#trace-end-pill"


def trace_overlays(trace: Widget) -> tuple[StickyReply, ScrollEndPill]:
    """Накладки над лентой `trace`: липкая реплика и плашка «в конец»."""
    return (
        StickyReply(trace, id=_SEL_STICKY[1:]),
        ScrollEndPill(trace, id=_SEL_PILL[1:]),
    )


def _is_top_reply(widget: Widget) -> bool:
    return isinstance(widget, UserReplyContainer)


class TraceFollowMixin:
    """Лента у низа, детект ухода вверх и накладки «не внизу»."""

    _STICK_TOL = 2

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._stick = True  # намерение «липнуть к низу» (_stick_bottom)
        self._last_auto_y = 0  # куда в прошлый раз программно прижали ленту
        # Пропустившие кадр за экраном (`note_missed_frame`); снятые
        # уходят сами.
        self._missed_frames: weakref.WeakSet[Widget] = weakref.WeakSet()

    def _watch_trace_follow(self) -> None:
        """Отклик на прокрутку ленты и на выделение — по событию."""
        self.watch(
            self.query_one(_SEL_TRACE, VerticalScroll),
            "scroll_y",
            self._on_trace_scrolled,
            init=False,
        )
        textual_compat.watch_selecting(self, self._on_select_freeze)
        self.screen_layout_refresh_signal.subscribe(
            self, self._on_layout_refreshed
        )

    def note_missed_frame(self, widget: Widget) -> None:
        """`widget` пропустил кадр за экраном: освежить, когда станет
        виден (`redraw_if_missed`)."""
        self._missed_frames.add(widget)

    def _on_layout_refreshed(self, _screen) -> None:
        """После раскладки (в том числе кадра прокрутки) карта видимых
        свежая: пропустившие кадр и ставшие видимыми перерисовываются и
        выходят из множества, снятые — тоже. Петли нет: перерисовка
        того же размера раскладку не запускает, а после перекладки
        строки в множестве уже нет. Во время выделения (F2) — ничего:
        лента под ним может ехать, догонит конец выделения."""
        if self._ui_frozen_for_select():
            return
        for widget in list(self._missed_frames):
            if not widget.is_attached or widget.redraw_if_missed():
                self._missed_frames.discard(widget)

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
        Зовётся на каждый шаг прокрутки и в тике опроса. Реплика ищется по
        раскладке самой ленты (П5): координаты содержимого из её кэша, без
        геометрии экрана — чтение невидимых строк пересобрало бы полную
        карту виджетов. Геометрия читается, только когда накладки
        показывают или прячут."""
        try:
            trace = self.query_one(_SEL_TRACE, VerticalScroll)
            sticky = self.query_one(_SEL_STICKY, StickyReply)
            pill = self.query_one(_SEL_PILL, ScrollEndPill)
        except NoMatches:
            return
        if self._stick_bottom and not (sticky.display or pill.display):
            return
        # Коробка накладок — ширина подложки реплики в ленте: без колонки
        # скроллбара (решение 27). Плашка центруется по ней же.
        box = trace.scrollable_content_region
        if self._stick_bottom:
            sticky.show(None, box)
            pill.show(None, box)
            return
        # Размер — ровно тот, что ленте передаёт компоновщик: попадание в
        # кэш раскладки. Иной размер пересчитал бы её и сбил якорь
        # (`AnchoredScroll.arrange`).
        placements = trace.arrange(box.size).placements
        reply = self._sticky_reply(placements, round(trace.scroll_y))
        sticky.show(reply, box)
        pill.show(self._end_pill_label(), box, self._pill_row(trace))

    def _sticky_reply(self, placements, scroll_y: int) -> Widget | None:
        """Реплика липкой строки: ближайшая смонтированная выше края
        (`placements` — раскладка ленты). Окно ленты достаёт реплику хода
        из модели, когда её шаг снят (`TraceWindowMixin`)."""
        return reply_above(placements, scroll_y, _is_top_reply)

    def _pill_row(self, trace: VerticalScroll) -> int:
        """Ряд плашки «в конец» — последний ряд ленты."""
        return trace.region.bottom - 1

    def _end_pill_label(self) -> str:
        """Подпись плашки: без счётчика новых реплик и без Enter."""
        return pill_label(0, enter=False)

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
        к низу не тянем — сначала детект, потом якорь. Пропустившие кадр
        и ставшие видимыми за время выделения освежаются сразу: конец
        выделения раскладки не запускает."""
        if not selecting:
            self._update_stick_state()
            self._on_layout_refreshed(self)
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
