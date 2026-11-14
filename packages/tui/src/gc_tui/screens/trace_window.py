"""Окно ленты — какие ходы смонтированы (спека шага 2 окна ленты, §3.1,
§5.3). Общее экранов ленты: превью и экран сессии.

Открытие монтирует хвост (`_window_mount_tail`). Пока лента отлистана
пользователем и до верха смонтированного меньше запаса, сверху
домонтируется кусок истории — под удержанием отрисовки до монтажа, а
`AnchoredScroll` держит первую видимую строку на месте внутри раскладки:
ни сдвига, ни прыжка на кадр. Пока лента у низа или идёт выделение —
ничего. ↑ в режиме событий на верхнем краю домонтирует кусок и переводит
фокус (`_window_edge_up`). По возвращении вниз окно ужимается до лимита
(`_window_on_bottom`), а лента, растущая живым путём у низа, снимает
старые ходы (`_window_appended`). Числа и решения —
`services.trace_window_policy`.

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
    from gc_tui.services.trace_ephemera import Ephemera


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
    # Дописано в конец с прошлой сверки с лимитом — по оценке виджетов.
    _window_grown = 0
    # Сверка с лимитом после роста уже ждёт раскладки.
    _window_grow_queued = False

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

    def _window_ephemera(self) -> Ephemera | None:
        """Память эфемерного экрана: снятые ходы возвращаются с ней."""
        return None

    def _window_adopt_chunk(self, refs: ChunkRefs) -> None:
        """Кусок сверху смонтирован: принять его ссылки (виды блоков)."""

    # ---- хвост и куски ---------------------------------------------------

    def _window_mount_tail(self, stop: int | None = None) -> ChunkRefs:
        """Новое окно: последние ходы на ``TAIL_MIN`` виджетов в конец
        ленты. Прежнее поколение окна больше не действует. ``stop`` —
        ходы от него рисует сам экран (незаконченный ход — живым путём);
        None — хвост до конца модели."""
        self._window_gen += 1
        self._window_busy = False
        self._window_stuck = True
        self._window_trim_due = False
        self._window_grown = 0
        self._window_heads = {}
        self._window_feed().hold_first_visible(None)
        model = self._window_model()
        estimates = self._window_estimates(model)[:stop]
        turns = policy.tail_turns(estimates, policy.TAIL_MIN)
        self._window_first = turns.start
        refs = self._window_render(turns, before=None)
        # Догрузка решает по высоте разложенного хвоста.
        self._window_feed().call_after_layout(self._window_fill)
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
            ephemera=self._window_ephemera(),
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
        кусок сверху домонтируется и у низа: якорь держит вид. Решает по
        высоте после раскладки хвоста."""
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
        feed.hold_first_visible(self._window_reading_row())
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
        if self._stick_bottom:
            # Лента выросла у низа (догрузка открытия): низ — заново и
            # запомнен, иначе уход вверх меряется от прежнего низа и
            # прилипание не снимается.
            self._scroll_trace_end()
        self.call_after_refresh(self._window_prefetch)
        self.call_after_refresh(self._window_fill)

    def _window_top(self) -> Widget | None:
        """Первый виджет окна в ленте."""
        heads = self._window_heads
        return heads[min(heads)] if heads else None

    def _window_reading_row(self) -> Widget | None:
        """Строка, которую держит якорь: первая видимая. Видимое выше окна
        (шапка начала сессии) — не строка чтения: кусок встаёт между ним и
        окном, держится первая строка окна, иначе содержимое под шапкой
        уезжает, а лента остаётся у верха — и следующий кусок, и так до
        начала истории."""
        feed = self._window_feed()
        row, top = feed.first_visible(), self._window_top()
        if row is None or top is None or row is top:
            return row
        children = list(feed.children)
        above = (
            row in children
            and top in children
            and children.index(row) < children.index(top)
        )
        return top if above else row

    # ---- ужатие у низа ---------------------------------------------------

    def _window_on_bottom(self) -> None:
        """Лента вернулась к низу: накопленное при чтении вверх ужимается
        до лимита целыми ходами — с запасом против петли с домонтированием
        (§3.1, вариант А). Пока идёт выделение — нет; пока встаёт кусок —
        когда встанет (если лента всё ещё у низа)."""
        if self._ui_frozen_for_select():
            return
        self._window_trim_due = self._window_busy
        if not self._window_busy:
            self._window_trim()

    def _window_appended(self, turn: int, widget: Widget) -> None:
        """Живой путь дописал ``widget`` хода ``turn`` (последнего в
        модели). Сверка с лимитом — не на каждый виджет (это пересчёт
        окна): с началом хода и после роста на кусок по оценке, после
        раскладки."""
        if turn < 0:
            return
        new_turn = turn not in self._window_heads
        if new_turn:
            self._window_heads[turn] = widget
        self._window_grown += policy.ITEM_WIDGETS
        if not new_turn and self._window_grown < policy.CHUNK_MIN:
            return
        self._window_grown = 0
        if not self._window_grow_queued:
            self._window_grow_queued = True
            self.call_after_refresh(self._window_on_grown)

    def _window_on_grown(self) -> None:
        """Лента выросла у низа: снять старые ходы сверх лимита — только
        пока она прилипла к низу, без выделения и пока кусок не встаёт
        (читающему вверх окно ужмётся по возвращении)."""
        self._window_grow_queued = False
        if (
            self._window_busy
            or not self._stick_bottom
            or self._ui_frozen_for_select()
        ):
            return
        self._window_trim()

    def _window_trim(self) -> None:
        """Ужать окно до лимита целыми ходами сверху, если оно больше
        «лимит + кусок» (запас против петли с домонтированием)."""
        turns, counts = self._window_mounted_counts()
        excess = policy.trim_to(
            sum(counts), policy.WINDOW_LIMIT, policy.CHUNK_MIN
        )
        if excess is None:
            return
        dropped = turns[: policy.drop_count(counts, excess)]
        if dropped:
            self._window_forget(range(self._window_first, dropped[-1] + 1))
            self.call_after_refresh(self._window_fill)

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

    def _window_forget(self, turns: range) -> None:
        """Снять ходы ``turns`` (первые в окне) по одному сверху; ход,
        который снять нельзя, останавливает снятие."""
        for turn in turns:
            if not self._window_forget_turn(turn):
                return

    def _window_forget_turn(self, turn: int) -> bool:
        """Снять верхний ход окна целиком; не верхний — False."""
        if turn != self._window_first:
            return False
        widgets = self._window_turn_widgets(turn)
        self._window_heads.pop(turn, None)
        self._window_first = turn + 1
        self._window_feed().remove_children(widgets)
        return True

    def _window_turn_widgets(self, turn: int) -> list[Widget]:
        """Виджеты ленты хода ``turn``: от его первого виджета до первого
        виджета следующего смонтированного хода. Ход без виджетов —
        пусто."""
        heads = self._window_heads
        start = heads.get(turn)
        if start is None:
            return []
        later = [t for t in heads if t > turn]
        stop = heads[min(later)] if later else None
        widgets: list[Widget] = []
        for widget in self._window_feed().children:
            if widget is stop:
                break
            if widgets or widget is start:
                widgets.append(widget)
        return widgets

    # ---- отклик на прокрутку ---------------------------------------------

    def _on_trace_scrolled(self, value: float) -> None:
        """Прокрутка пользователем: удерживать первую видимую строку,
        пока лента не у низа, и домонтировать сверху, если пора."""
        super()._on_trace_scrolled(value)
        if self._ui_frozen_for_select():
            return
        feed = self._window_feed()
        feed.hold_first_visible(
            None if self._stick_bottom else self._window_reading_row()
        )
        self._window_prefetch()
        # Лента стала ниже запаса (свёртка, снятие) — догрузить сверху.
        self._window_fill()

    def _sync_trace_anchor(self) -> None:
        """Смена липкости: вернулась к низу — ужатие окна."""
        super()._sync_trace_anchor()
        stuck = self._stick_bottom and not self._ui_frozen_for_select()
        returned = stuck and not self._window_stuck
        self._window_stuck = stuck
        if returned:
            self._window_on_bottom()
