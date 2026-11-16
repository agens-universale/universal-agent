"""Окно ленты — какие ходы смонтированы (спека шага 2 окна ленты, §3.1,
§5.3, §5.11). Общее экранов ленты: превью и экран сессии.

Смонтированы прочитанный диапазон ``[first, gap)`` и закреплённый низ
``[tail, N)`` (живой ход и примыкающие закреплённые; в превью — пусто);
разрыв ``_window_gap = [gap, tail)`` открыт, когда диапазоны не сходятся.
Открытие монтирует хвост (`_window_mount_tail`). Пока лента отлистана
пользователем, к краю, до которого меньше запаса, домонтируется кусок —
сверху или перед разрывом — под удержанием отрисовки, а `AnchoredScroll`
держит читаемую строку на месте внутри раскладки. Встал кусок — снимается
противоположный край: целые ходы дальше двух запасов от видимого, до
закреплённого хода. ↑ / ↓ в режиме событий на краю домонтируют и переводят
фокус (`_window_edge_up`, `_window_edge_down`). Пока разрыв открыт, лента
не «у низа»; переход к низу (`_window_on_bottom`) или читаемая строка,
ушедшая в закреплённый низ, закрывают разрыв (`_window_close_gap`). У низа
лента, растущая живым путём, снимает старые ходы (`_window_appended`).
Числа и решения — `services.trace_window_policy`.

Экран ставит миксин перед `TraceFollowMixin` и даёт ленту
(`_window_feed`) и модель истории (`_window_model`); параметры отрисовки,
вложенный трейс, приём кусков, закреплённые ходы и уход фокуса — по
желанию. Смена сессии — новый хвост: поколение окна (`_window_gen`)
растёт, результат куска прежнего поколения выбрасывается.
"""

from __future__ import annotations

from itertools import takewhile
from typing import TYPE_CHECKING, Literal

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

#: Край окна, у которого встал кусок: сверху, перед разрывом или хвост
#: при закрытии разрыва.
Side = Literal["top", "bottom", "end"]
#: Край, с которого снимаются ходы.
Edge = Literal["top", "bottom"]


class TraceWindowMixin:
    """Окно ленты: хвост, домонтирование и снятие с краёв, разрыв."""

    # Поколение окна: растёт с каждым новым хвостом (смена сессии).
    _window_gen = 0
    # Первый смонтированный ход; ходы от него до конца модели, кроме
    # разрыва, — в ленте.
    _window_first = 0
    # Разрыв: несмонтированные ходы между прочитанным и закреплённым низом;
    # пустой — разрыва нет.
    _window_gap = range(0)
    # Кусок встаёт (смонтирован, ещё не собран).
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
        """Кусок смонтирован: принять его ссылки (виды блоков)."""

    def _window_pinned(self, turn: int) -> bool:
        """Ход снимать нельзя (§5.3): снятие с края на нём
        останавливается."""
        return False

    def _window_focus_home(self) -> None:
        """Явный переход в конец: фокус со старой строки — домой (§5.11)."""
        self.set_focus(None)

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
        self._window_gap = range(0)
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
        """Домонтировать кусок к краю, если пора: лента отлистана
        пользователем (не прилипла к низу), выделения нет, до верха или до
        разрыва смонтированного меньше запаса. Читаемая строка ушла за
        разрыв в закреплённый низ — разрыв закрывается."""
        if (
            self._window_busy
            or self._stick_bottom
            or self._ui_frozen_for_select()
        ):
            return
        feed = self._window_feed()
        height = feed.container_size.height
        if self._window_first > 0 and policy.near_edge(feed.scroll_y, height):
            self._window_prepend(focus=False)
            return
        gap_y = self._window_gap_y() if self._window_gap else None
        if gap_y is None:
            return
        if gap_y <= feed.scroll_y:
            if not self._window_island():
                self._window_close_gap()
        elif policy.near_edge(gap_y - feed.scroll_y - height, height):
            self._window_append(focus=False)

    def _window_fill(self) -> None:
        """Часть открытия: хвост по оценке мог не заполнить ленту (блоки
        свёрнуты, ходы короткие) — тогда прокрутить нельзя и история выше
        недостижима. Пока ленте не хватает запаса ``PREFETCH_SCREENS``,
        кусок домонтируется — перед разрывом, если он открыт, иначе
        сверху и у низа: якорь держит вид. Решает по высоте после
        раскладки."""
        if self._window_busy or self._ui_frozen_for_select():
            return
        feed = self._window_feed()
        if not policy.underfilled(
            feed.max_scroll_y, feed.container_size.height
        ):
            return
        if self._window_gap:
            self._window_append(focus=False)
        elif self._window_first > 0:
            self._window_prepend(focus=False)

    def _window_edge_up(self) -> bool:
        """↑ в режиме событий на верхнем краю смонтированного: кусок
        сверху и фокус на его последнее событие. ``False`` — домонтировать
        нечего (начало истории); кусок уже встаёт — клавиша съедена."""
        if self._window_busy:
            return True
        return self._window_prepend(focus=True)

    def _window_edge_down(
        self, current: Widget, following: Widget | None
    ) -> bool:
        """↓ в режиме событий с ``current`` на ``following`` (следующее
        событие ленты, None — нет). Край разрыва — ``current`` перед
        разрывом, ``following`` за ним: кусок перед разрывом и фокус на
        его первое событие. ``False`` — не край разрыва; кусок уже встаёт
        — клавиша съедена."""
        if (
            not self._window_gap
            or self._window_past_gap(current)
            or not self._window_past_gap(following)
        ):
            return False
        if self._window_busy:
            return True
        return self._window_append(focus=True)

    def _window_prepend(self, *, focus: bool) -> bool:
        model = self._window_model()
        turns = policy.prev_chunk(
            self._window_first, self._window_estimates(model), policy.CHUNK_MIN
        )
        if not turns:
            return False
        refs = self._window_insert(turns, before=self._window_top())
        self._window_first = turns.start
        self._window_settle_later(refs, "top", focus)
        return True

    def _window_append(self, *, focus: bool) -> bool:
        """Кусок перед разрывом (§5.11) — зеркально `_window_prepend`."""
        gap = self._window_gap
        turns = policy.next_chunk(
            gap.start,
            gap.stop,
            self._window_estimates(self._window_model()),
            policy.CHUNK_MIN,
        )
        if not turns:
            return False
        refs = self._window_insert(turns, before=self._window_gap_head())
        self._window_set_gap(turns.stop, gap.stop)
        self._window_settle_later(refs, "bottom", focus)
        return True

    def _window_insert(
        self, turns: range, *, before: Widget | None
    ) -> ChunkRefs:
        """Ходы ``turns`` — перед ``before``; якорь держит читаемую
        строку."""
        self._window_feed().hold_first_visible(self._window_reading_row())
        refs = self._window_render(turns, before=before)
        self._window_adopt_chunk(refs)
        return refs

    def _window_settle_later(
        self, refs: ChunkRefs, side: Side, focus: bool
    ) -> None:
        """Кусок встаёт под удержанием отрисовки до монтажа; потом —
        `_window_settle` очередью ленты: после того, как лента разобрала
        сообщения монтажа куска. Иначе подъём кусок за куском обгоняет её
        очередь, а ждущие в ней сообщения держат снятые ходы."""
        self._window_busy = True
        gen = self._window_gen
        task = textual_compat.paint_after_mount(*refs.widgets)
        if task is None:
            self._window_settle(gen, refs.widgets, side, focus)
            return
        # Лента — сейчас, пока экран жив: отпускание приходит из цикла
        # asyncio, а снятый к тому времени экран ленты уже не найдёт.
        # Закрытая очередь ленты просто не примет сообщение.
        feed = self._window_feed()
        task.add_done_callback(
            lambda _t: feed.call_later(
                self._window_settle, gen, refs.widgets, side, focus
            )
        )

    def _window_settle(
        self, gen: int, widgets: list[Widget], side: Side, focus: bool
    ) -> None:
        """Кусок встал на краю ``side`` (``top``, ``bottom`` или ``end`` —
        закрытие разрыва). Окно сменилось (другая сессия) — результат
        выброшен. Пока вставал, лента вернулась к низу — переход к низу
        сейчас; отлистана — снимается противоположный край; запас ещё
        мал — следующий кусок после раскладки."""
        if gen != self._window_gen:
            return
        self._window_busy = False
        if focus:
            events = [w for w in widgets if w.can_focus and w.display]
            if events:
                events[-1 if side == "top" else 0].focus()
        if self._window_trim_due and self._window_stuck:
            self._window_on_bottom()
        elif not self._stick_bottom:
            # Снятие — сразу, как кусок встал, до следующего куска: иначе
            # при подъёме подряд следующий кусок всякий раз опережает
            # снятие. Раскладка прошлая и прокрутка прошлая — согласованы;
            # новый кусок на своём краю, снятие идёт с другого и
            # останавливается на удерживаемом.
            self._window_trim("bottom" if side == "top" else "top")
        elif side == "end":
            self._window_trim("top")
        if self._stick_bottom:
            # Лента выросла у низа (догрузка открытия, хвост перехода в
            # конец): низ — заново и запомнен, иначе уход вверх меряется
            # от прежнего низа и прилипание не снимается.
            self._scroll_trace_end()
        self.call_after_refresh(self._window_prefetch)
        self.call_after_refresh(self._window_fill)

    def _window_top(self) -> Widget | None:
        """Первый виджет окна в ленте."""
        heads = self._window_heads
        return heads[min(heads)] if heads else None

    def _window_gap_head(self) -> Widget | None:
        """Первый виджет закреплённого низа за разрывом; нет — None
        (превью: разрыв до конца истории)."""
        heads = self._window_heads
        later = [t for t in heads if t >= self._window_gap.stop]
        return heads[min(later)] if later else None

    def _window_gap_y(self) -> float | None:
        """Строка разрыва в ленте: верх закреплённого низа или конец
        содержимого; раскладки ещё нет — None."""
        feed = self._window_feed()
        head = self._window_gap_head()
        if head is None:
            return feed.virtual_size.height
        return feed.placed_top(head)

    def _window_past_gap(self, widget: Widget | None) -> bool:
        """``widget`` (ребёнок ленты) — за разрывом; None — конец ленты."""
        if widget is None:
            return True
        head = self._window_gap_head()
        if head is None:
            return False
        children = list(self._window_feed().children)
        return children.index(widget) >= children.index(head)

    def _window_set_gap(self, start: int, stop: int) -> None:
        self._window_gap = range(start, stop) if start < stop else range(0)

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

    # ---- низ и переход в конец -------------------------------------------

    def _update_stick_state(self) -> None:
        """Пока разрыв открыт, физический низ — не конец истории: касание
        низа липкость не включает (§5.11). Кроме острова: закрыть разрыв
        нельзя — он низу не мешает. Снятие липкости — как раньше, по
        физическому низу: переход в конец, ещё не закрывший разрыв, иначе
        снимался бы прокруткой собственного якоря."""
        if (
            not self._stick_bottom
            and self._window_gap
            and not self._window_island()
        ):
            return
        super()._update_stick_state()

    def _window_island(self) -> bool:
        """Остров: в прочитанном остались только закреплённые ходы —
        снять нечего, разрыв под ними не закрыть."""
        return all(
            self._window_pinned(turn)
            for turn in range(self._window_first, self._window_gap.start)
        )

    def _window_on_bottom(self) -> None:
        """Переход к низу — одна точка всех путей (отправка, команда,
        плашка, клавиша): разрыв открыт — фокус со старой строки домой и
        разрыв закрывается; нет — ужатие сверху до лимита целыми ходами с
        запасом против петли с домонтированием. Пока идёт выделение —
        нет; пока встаёт кусок — когда встанет (если лента всё ещё у
        низа)."""
        if self._ui_frozen_for_select():
            return
        self._window_trim_due = self._window_busy
        if self._window_busy:
            return
        if self._window_gap:
            if self._window_in_read_range(self.focused):
                self._window_focus_home()
            self._window_close_gap()
        else:
            self._window_trim("top")

    def _window_close_gap(self) -> None:
        """Закрыть разрыв (§5.11): короткий — домонтировать целиком;
        длинный — снять прочитанный диапазон и смонтировать перед
        закреплённым низом хвост, как при открытии. Одним удержанием
        отрисовки, без смены поколения; якорь держит читаемую строку
        (у низа — низ). Ход, который снять нельзя, остаётся."""
        gap = self._window_gap
        estimates = self._window_estimates(self._window_model())
        if not policy.short_gap(sum(estimates[gap.start : gap.stop])):
            self._window_forget_read_range()
        gap = self._window_gap
        stop = gap.stop if gap else self._window_first
        # Хвост, как при открытии, — с закреплённым низом в бюджете.
        tail = policy.tail_turns(estimates, policy.TAIL_MIN)
        low = gap.start if gap else 0
        if policy.short_gap(sum(estimates[low:stop])):
            tail = range(low, stop)
        turns = range(max(tail.start, low), stop)
        # Как при открытии: хвост ниже запаса ленты — догрузка после
        # раскладки.
        self._window_feed().call_after_layout(self._window_fill)
        if not turns:
            return
        before = self._window_gap_head() if gap else self._window_top()
        refs = self._window_insert(turns, before=before)
        if gap:
            self._window_set_gap(gap.start, turns.start)
        else:
            self._window_first = turns.start
        self._window_settle_later(refs, "end", False)

    def _window_forget_read_range(self) -> None:
        """Снять прочитанный диапазон с обоих концов, до закреплённых."""
        for edge in ("top", "bottom"):
            while self._window_gap:
                gap = self._window_gap
                turn = self._window_first if edge == "top" else gap.start - 1
                if not self._window_forget_turn(turn):
                    break

    def _window_in_read_range(self, widget: Widget | None) -> bool:
        """``widget`` в ленте выше разрыва."""
        feed = self._window_feed()
        top = next(
            (
                w
                for w in getattr(widget, "ancestors_with_self", ())
                if w.parent is feed
            ),
            None,
        )
        return top is not None and not self._window_past_gap(top)

    # ---- снятие ----------------------------------------------------------

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
        пока она прилипла к низу, без выделения и пока кусок не встаёт.
        Рост живого хода при чтении снятия не запускает (§5.11): его
        проверит следующий кусок или переход в конец."""
        self._window_grow_queued = False
        if self._stick_bottom and not self._window_busy:
            self._window_trim("top")

    def _window_trim(self, edge: Edge) -> None:
        """Ужать окно до лимита целыми ходами с края ``edge`` (``top`` /
        ``bottom``), если оно больше «лимит + кусок» (запас против петли с
        домонтированием). Закреплённое считается в лимите; снимаются ходы
        от края до удерживаемых (видимое с запасами) или закреплённого."""
        if self._ui_frozen_for_select() or (
            # Низ снимается только при чтении: у низа там живой ход.
            edge == "bottom" and self._stick_bottom
        ):
            return
        if self._window_busy:
            return
        turns, counts = self._window_mounted_counts()
        excess = policy.trim_to(
            sum(counts), policy.WINDOW_LIMIT, policy.CHUNK_MIN
        )
        if excess is None:
            return
        kept = self._window_kept_turns()
        if kept is None:
            return
        order = self._window_edge_order(edge, list(zip(turns, counts)))
        removable = list(
            takewhile(
                lambda m: m[0] not in kept and not self._window_pinned(m[0]),
                order,
            )
        )
        dropped = removable[
            : policy.edge_drops([c for _t, c in removable], excess)
        ]
        if not dropped:
            return
        if edge == "top" and not self._stick_bottom:
            self._window_feed().hold_first_visible(self._window_reading_row())
        for turn, _count in dropped:
            if not self._window_forget_turn(turn):
                break
        self.call_after_refresh(self._window_fill)

    def _window_edge_order(
        self, edge: Edge, mounted: list[tuple[int, int]]
    ) -> list[tuple[int, int]]:
        """Смонтированные ходы от края ``edge`` внутрь прочитанного
        диапазона. Снизу: при разрыве — сначала незакреплённые ходы в
        начале закреплённого низа (переставшие быть живыми), потом низ
        прочитанного; без разрыва — мимо закреплённых ходов у низа."""
        gap = self._window_gap
        read = [m for m in mounted if not gap or m[0] < gap.start]
        if edge == "top":
            return read
        lead: list[tuple[int, int]] = []
        if gap:
            for m in (m for m in mounted if m[0] >= gap.stop):
                if self._window_pinned(m[0]):
                    break
                lead.append(m)
            return [*lead, *reversed(read)]
        rest = list(reversed(read))
        while rest and self._window_pinned(rest[0][0]):
            rest.pop(0)
        return rest

    def _window_kept_turns(self) -> set[int] | None:
        """Ходы, задевающие видимое с запасами (`policy.keep_rows`).
        Раскладки не было — None; у низа — только последний ход, как
        раньше."""
        feed = self._window_feed()
        rows = policy.keep_rows(
            feed.scroll_y,
            feed.container_size.height,
            stuck=self._stick_bottom,
        )
        widgets = feed.placed_between(*rows)
        if widgets is None:
            if not self._stick_bottom:
                return None
            widgets = set()
        turn_at = {w: t for t, w in self._window_heads.items()}
        kept: set[int] = set()
        turn = None
        for widget in feed.children:
            turn = turn_at.get(widget, turn)
            if turn is not None and widget in widgets:
                kept.add(turn)
        if not kept and self._window_heads:
            # Удерживаемых нет — последний ход остаётся, даже гигантский.
            kept.add(max(self._window_heads))
        return kept

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

    def _window_removable(self, turn: int) -> bool:
        """Ход на конце прочитанного диапазона: верхний или нижний перед
        разрывом, либо первый за разрывом; без разрыва — любой
        смонтированный (снятие откроет разрыв на нём)."""
        gap = self._window_gap
        if turn == self._window_first:
            return True
        if gap:
            return turn in (gap.start - 1, gap.stop)
        return turn in self._window_heads

    def _window_forget_turn(self, turn: int) -> bool:
        """Снять ход на конце прочитанного диапазона целиком; не на конце
        — False."""
        if not self._window_removable(turn):
            return False
        widgets = self._window_turn_widgets(turn)
        self._window_heads.pop(turn, None)
        self._window_drop(turn)
        self._window_feed().remove_children(widgets)
        return True

    def _window_drop(self, turn: int) -> None:
        """Границы окна без снятого хода ``turn``. Прочитанный диапазон
        снят целиком — окно сходится к закреплённому низу."""
        gap = self._window_gap
        if turn == self._window_first:
            self._window_first = turn + 1
            if gap and self._window_first == gap.start:
                self._window_first, self._window_gap = gap.stop, range(0)
        elif not gap:
            self._window_gap = range(turn, turn + 1)
        elif turn == gap.start - 1:
            self._window_gap = range(turn, gap.stop)
        else:
            self._window_gap = range(gap.start, turn + 1)

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
        пока лента не у низа, и домонтировать к краю, если пора."""
        super()._on_trace_scrolled(value)
        if self._ui_frozen_for_select():
            return
        feed = self._window_feed()
        feed.hold_first_visible(
            None if self._stick_bottom else self._window_reading_row()
        )
        self._window_prefetch()
        # Лента стала ниже запаса (свёртка, снятие) — догрузить.
        self._window_fill()

    def _sync_trace_anchor(self) -> None:
        """Смена липкости: вернулась к низу — переход к низу окна."""
        super()._sync_trace_anchor()
        stuck = self._stick_bottom and not self._ui_frozen_for_select()
        returned = stuck and not self._window_stuck
        self._window_stuck = stuck
        if returned:
            self._window_on_bottom()
