"""Окно ленты — какие ходы смонтированы (спека шага 2 окна ленты, §3.1,
§5.3). Общее экранов ленты: превью и экран сессии.

Открытие монтирует хвост (`_window_mount_tail`). Пока лента отлистана
пользователем и до верха смонтированного меньше запаса, сверху
домонтируется кусок истории — под удержанием отрисовки до монтажа, а
`AnchoredScroll` держит первую видимую строку на месте внутри раскладки:
ни сдвига, ни прыжка на кадр. Пока лента у низа или идёт выделение —
ничего. ↑ в режиме событий на верхнем краю домонтирует кусок и переводит
фокус (`_window_edge_up`). По возвращении вниз окно ужимается до лимита
(`_window_on_bottom`). Числа и решения — `services.trace_window_policy`.

Экран ставит миксин перед `TraceFollowMixin` и даёт ленту
(`_window_feed`) и модель истории (`_window_model`); параметры отрисовки,
вложенный трейс и приём кусков — по желанию. Смена сессии — новый хвост:
поколение окна (`_window_gen`) растёт, результат куска прежнего поколения
выбрасывается.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gc_tui import textual_compat
from gc_tui.services import trace_window_policy as policy
from gc_tui.services.history_render import (
    ChunkOptions,
    FeedHost,
    render_turns,
)

if TYPE_CHECKING:
    from textual.widget import Widget

    from gc_tui.services.history_model import HistoryModel
    from gc_tui.services.history_render import (
        ChunkRefs,
        HistoryHost,
        NestedSource,
    )


class TraceWindowMixin:
    """Окно ленты: хвост, домонтирование сверху, ужатие у низа."""

    # Поколение окна: растёт с каждым новым хвостом (смена сессии).
    _window_gen = 0
    # Первый смонтированный ход; ходы от него до конца модели — в ленте.
    _window_first = 0
    # Кусок сверху встаёт (смонтирован, ещё не собран).
    _window_busy = False
    # Лента прилипла к низу — на прошлой смене липкости.
    _window_stuck = True
    # Вернулась к низу, пока кусок вставал: ужатие — когда встанет.
    _window_trim_due = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Первый в ленте виджет каждого смонтированного хода.
        self._window_heads: dict[int, Widget] = {}

    # ---- что даёт экран --------------------------------------------------

    def _window_feed(self) -> Widget:
        """Лента окна (`AnchoredScroll`)."""
        raise NotImplementedError

    def _window_model(self) -> HistoryModel:
        """Модель истории открытой сессии."""
        raise NotImplementedError

    def _window_host(self) -> HistoryHost:
        return FeedHost(self._window_feed())

    def _window_options(self) -> ChunkOptions:
        return ChunkOptions()

    def _window_nested(self) -> NestedSource | None:
        return None

    def _window_adopt_chunk(self, refs: ChunkRefs) -> None:
        """Кусок сверху смонтирован: принять его ссылки (виды блоков)."""

    # ---- хвост и куски ---------------------------------------------------

    def _window_mount_tail(self) -> ChunkRefs:
        """Новое окно: последние ходы на ``TAIL_MIN`` виджетов в конец
        ленты. Прежнее поколение окна больше не действует."""
        self._window_gen += 1
        self._window_busy = False
        self._window_stuck = True
        self._window_trim_due = False
        self._window_heads = {}
        self._window_feed().hold_first_visible(None)
        model = self._window_model()
        turns = policy.tail_turns(
            self._window_estimates(model), policy.TAIL_MIN
        )
        self._window_first = turns.start
        refs = self._window_render(turns, before=None)
        self.call_after_refresh(self._window_fill)
        return refs

    def _window_render(
        self, turns: range, *, before: Widget | None
    ) -> ChunkRefs:
        refs = render_turns(
            self._window_host(),
            self._window_model(),
            turns,
            before=before,
            options=self._window_options(),
            nested=self._window_nested(),
        )
        self._window_heads.update(refs.turn_heads)
        return refs

    @staticmethod
    def _window_estimates(model: HistoryModel) -> list[int]:
        """Оценка виджетов каждого хода модели — до монтажа."""
        starts = model.turn_starts
        ends = [*starts[1:], len(model.items)][: len(starts)]
        return [
            (end - start) * policy.ITEM_WIDGETS
            for start, end in zip(starts, ends, strict=True)
        ]

    def _window_prefetch(self) -> None:
        """Домонтировать кусок сверху, если пора: лента отлистана
        пользователем (не прилипла к низу), выделения нет, до верха
        смонтированного меньше запаса."""
        if (
            self._window_busy
            or self._window_first <= 0
            or self._stick_bottom
            or self._ui_frozen_for_select()
        ):
            return
        feed = self._window_feed()
        if policy.near_top(feed.scroll_y, feed.container_size.height):
            self._window_prepend(focus=False)

    def _window_fill(self) -> None:
        """Часть открытия: хвост по оценке мог не заполнить ленту (блоки
        свёрнуты, ходы короткие) — тогда прокрутить нельзя и история выше
        недостижима. Пока ленте не хватает запаса ``PREFETCH_SCREENS``,
        кусок сверху домонтируется и у низа: якорь держит вид."""
        if (
            self._window_busy
            or self._window_first <= 0
            or self._ui_frozen_for_select()
        ):
            return
        feed = self._window_feed()
        if policy.underfilled(feed.max_scroll_y, feed.container_size.height):
            self._window_prepend(focus=False)

    def _window_edge_up(self) -> bool:
        """↑ в режиме событий на верхнем краю смонтированного: кусок
        сверху и фокус на его последнее событие. ``False`` — домонтировать
        нечего (начало истории); кусок уже встаёт — клавиша съедена."""
        if self._window_busy:
            return True
        return self._window_prepend(focus=True)

    def _window_prepend(self, *, focus: bool) -> bool:
        model = self._window_model()
        first = self._window_first
        turns = policy.prev_chunk(
            first, self._window_estimates(model), policy.CHUNK_MIN
        )
        if not turns:
            return False
        feed = self._window_feed()
        feed.hold_first_visible(feed.first_visible())
        refs = self._window_render(turns, before=self._window_top())
        self._window_first = turns.start
        self._window_adopt_chunk(refs)
        self._window_busy = True
        gen = self._window_gen
        task = textual_compat.paint_after_mount(*refs.widgets)
        if task is None:
            self._window_settle(gen, refs.widgets, focus)
        else:
            task.add_done_callback(
                lambda _t: self._window_settle(gen, refs.widgets, focus)
            )
        return True

    def _window_settle(
        self, gen: int, widgets: list[Widget], focus: bool
    ) -> None:
        """Кусок встал. Окно сменилось (другая сессия) — результат
        выброшен. Пока вставал, лента вернулась к низу — ужатие сейчас;
        иначе запас ещё мал — следующий кусок после раскладки, когда
        прокрутка уже сдвинута."""
        if gen != self._window_gen:
            return
        self._window_busy = False
        if focus:
            events = [w for w in widgets if w.can_focus and w.display]
            if events:
                events[-1].focus()
        if self._window_trim_due and self._window_stuck:
            self._window_on_bottom()
        self.call_after_refresh(self._window_prefetch)
        self.call_after_refresh(self._window_fill)

    def _window_top(self) -> Widget | None:
        """Первый виджет окна в ленте."""
        heads = self._window_heads
        return heads[min(heads)] if heads else None

    # ---- ужатие у низа ---------------------------------------------------

    def _window_on_bottom(self) -> None:
        """Лента вернулась к низу: накопленное при чтении вверх ужимается
        до лимита целыми ходами — с запасом против петли с домонтированием
        (§3.1, вариант А). Пока идёт выделение — нет; пока встаёт кусок —
        когда встанет (если лента всё ещё у низа)."""
        if self._ui_frozen_for_select():
            return
        self._window_trim_due = self._window_busy
        if self._window_busy:
            return
        turns, counts = self._window_mounted_counts()
        excess = policy.trim_to(
            sum(counts), policy.WINDOW_LIMIT, policy.CHUNK_MIN
        )
        if excess is None:
            return
        dropped = turns[: policy.drop_count(counts, excess)]
        if dropped:
            self._window_forget(range(self._window_first, dropped[-1] + 1))

    def _window_mounted_counts(self) -> tuple[list[int], list[int]]:
        """Смонтированные ходы с виджетами и число виджетов поддерева
        ленты каждого: ход — от своего первого виджета до первого
        следующего."""
        turn_at = {w: t for t, w in self._window_heads.items()}
        turns: list[int] = []
        counts: list[int] = []
        for widget in self._window_feed().children:
            turn = turn_at.get(widget)
            if turn is not None:
                turns.append(turn)
                counts.append(0)
            if counts:
                counts[-1] += len(widget.walk_children(with_self=True))
        return turns, counts

    def _window_forget(self, turns: range) -> list[Widget]:
        """Снять ходы ``turns`` (первые в окне) целиком; снятые виджеты
        ленты — экрану, чтобы забыть ссылки на них."""
        heads = self._window_heads
        start = self._window_top()
        later = [t for t in heads if t >= turns.stop]
        stop = heads[min(later)] if later else None
        removed: list[Widget] = []
        for widget in self._window_feed().children:
            if widget is stop:
                break
            if removed or widget is start:
                removed.append(widget)
        for turn in turns:
            heads.pop(turn, None)
        self._window_first = turns.stop
        self._window_feed().remove_children(removed)
        return removed

    # ---- отклик на прокрутку ---------------------------------------------

    def _on_trace_scrolled(self, value: float) -> None:
        """Прокрутка пользователем: удерживать первую видимую строку,
        пока лента не у низа, и домонтировать сверху, если пора."""
        super()._on_trace_scrolled(value)
        if self._ui_frozen_for_select():
            return
        feed = self._window_feed()
        feed.hold_first_visible(
            None if self._stick_bottom else feed.first_visible()
        )
        self._window_prefetch()

    def _sync_trace_anchor(self) -> None:
        """Смена липкости: вернулась к низу — ужатие окна."""
        super()._sync_trace_anchor()
        stuck = self._stick_bottom and not self._ui_frozen_for_select()
        returned = stuck and not self._window_stuck
        self._window_stuck = stuck
        if returned:
            self._window_on_bottom()
