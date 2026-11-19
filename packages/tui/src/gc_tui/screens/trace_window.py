"""Окно ленты — какие шаги ходов смонтированы (спека шага 2 окна ленты,
§3.1, §5.3, §5.11). Общее экранов ленты: превью и экран сессии.

Единица окна — шаг хода (`HistoryModel.step_starts`, `trace_steps`):
гигантский ход агента (сотня вызовов тулов) домонтируется и снимается по
шагам, а не целиком; у короткого хода шаг один. Индексы окна ниже —
номера шагов модели.

Смонтированы прочитанный диапазон ``[first, gap)`` и закреплённый низ
``[tail, N)`` (последний шаг и примыкающие закреплённые — с живым
состоянием, `_window_pinned`; в превью — пусто); разрыв ``_window_gap = [gap, tail)`` открыт, когда диапазоны не
сходятся.
Открытие монтирует хвост (`_window_mount_tail`). Пока лента отлистана
пользователем, к краю, до которого меньше запаса, домонтируется кусок —
сверху или перед разрывом — под удержанием отрисовки, а `AnchoredScroll`
держит читаемую строку на месте внутри раскладки. Встал кусок — снимается
противоположный край: целые шаги дальше двух запасов от видимого, до
закреплённого шага. ↑ / ↓ в режиме событий на краю домонтируют и переводят
фокус (`_window_edge_up`, `_window_edge_down`). Пока разрыв открыт, лента
не «у низа»; переход к низу (`_window_on_bottom`) или читаемая строка,
ушедшая в закреплённый низ, закрывают разрыв (`_window_close_gap`). У низа
лента, растущая живым путём, снимает старые шаги (`_window_appended`).
Числа и решения — `services.trace_window_policy`.

Экран ставит миксин перед `TraceFollowMixin` и даёт ленту
(`_window_feed`) и модель истории (`_window_model`); параметры отрисовки,
вложенный трейс, приём кусков, закреплённые шаги и уход фокуса — по
желанию. Смена сессии — новый хвост: поколение окна (`_window_gen`)
растёт, результат куска прежнего поколения выбрасывается.
"""

from __future__ import annotations

import contextlib
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from itertools import takewhile
from typing import TYPE_CHECKING, Literal
from weakref import WeakKeyDictionary

from gc_tui import textual_compat
from gc_tui.services import trace_window_policy as policy
from gc_tui.services.history_render import (
    ChunkOptions,
    FeedHost,
    render_steps,
    reply_widget,
)
from gc_tui.services.trace_steps import shown

if TYPE_CHECKING:
    from textual.widget import Widget

    from gc_tui.messages import ScrollToReply
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
#: Край, с которого снимаются шаги.
Edge = Literal["top", "bottom"]


def step_heads(
    children: Iterable[Widget], step_of: Mapping[Widget, int]
) -> dict[int, Widget]:
    """Голова каждого шага — первый по ленте его виджет (метка шага
    ``step_of``), который не снимается: снятый мимо окна виджет передаёт
    роль следующему виджету шага, вставленный перед строкой шага
    (заголовок группы) сам становится головой. Метка не старше уже
    встреченной головы головы не даёт: такой виджет (прижатый хвост,
    помеченный до того, как над ним встали шаги новее) по ленте лежит в
    шаге перед ним."""
    heads: dict[int, Widget] = {}
    last = -1
    for widget in children:
        step = step_of.get(widget)
        if (
            step is None
            or step <= last
            or textual_compat.is_being_removed(widget)
        ):
            continue
        heads[step] = widget
        last = step
    return heads


class _Snapshot:
    """Шаги ленты на одну операцию окна (`TraceWindowMixin._window_scope`):
    головы — проходом при создании, виджеты каждого шага — одним проходом
    при первом обращении к ним."""

    def __init__(self, heads: dict[int, Widget], children) -> None:
        self.heads = heads
        self._children = children
        self._widgets: dict[int, list[Widget]] | None = None

    @property
    def widgets(self) -> dict[int, list[Widget]]:
        if self._widgets is None:
            self._widgets = {}
            step_at = {w: t for t, w in self.heads.items()}
            step = None
            for widget in self._children:
                step = step_at.get(widget, step)
                if step is not None:
                    self._widgets.setdefault(step, []).append(widget)
        return self._widgets

    def forget(self, step: int) -> None:
        """Шаг ``step`` снят окном целиком."""
        self.heads.pop(step, None)
        if self._widgets is not None:
            self._widgets.pop(step, None)


class TraceWindowMixin:
    """Окно ленты: хвост, домонтирование и снятие с краёв, разрыв."""

    # Поколение окна: растёт с каждым новым хвостом (смена сессии).
    _window_gen = 0
    # Первый смонтированный шаг; шаги от него до конца модели, кроме
    # разрыва, — в ленте.
    _window_first = 0
    # Разрыв: несмонтированные шаги между прочитанным и закреплённым низом;
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
    # Шаг последнего виджета, дописанного живым путём.
    _window_grow_step = -1
    # Операция окна идёт (`_window_scope`): шаги ленты считаются один раз
    # на операцию, снимок — в `_window_snap`.
    _window_scoped = 0
    _window_snap: _Snapshot | None = None
    # Шаг реплики, к которой ведёт клик по липкой строке из модели:
    # кусками, как подъём (`_window_reach_reply`); None — не ведёт. Строка,
    # куда путь прокрутил ленту сам: прокрутка в другое место — человек
    # ушёл, путь отменяется.
    _window_reply_step: int | None = None
    _window_reply_y: int | None = None

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Шаг каждого смонтированного виджета ленты (кусок и живой путь);
        # снятый виджет уходит сам.
        self._window_step_of: WeakKeyDictionary[Widget, int] = (
            WeakKeyDictionary()
        )
        # Реплики ходов из модели для липкой строки (номер события → виджет
        # вне ленты), когда шаг реплики снят (R46).
        self._window_replies: dict[int, Widget] = {}

    @property
    def _window_heads(self) -> dict[int, Widget]:
        """Голова каждого смонтированного шага — первый по ленте его
        виджет (`step_heads`), одно правило для кусков и живого пути.
        Снятое мимо окна (свёртка попыток сдачи, откат реплики) голову не
        уносит: она переходит к следующему виджету шага. У шага без
        виджетов головы нет; границы окна (`_window_first`, края разрыва)
        проматываются через такие шаги (`_window_skip_empty`). Внутри
        операции окна — из её снимка (`_window_scope`)."""
        return self._window_shot().heads

    def _window_shot(self) -> _Snapshot:
        """Шаги ленты: внутри операции окна — снимок на всю операцию,
        вне — заново."""
        snap = self._window_snap
        if snap is None:
            children = self._window_feed().children
            snap = _Snapshot(
                step_heads(children, self._window_step_of), children
            )
            if self._window_scoped:
                self._window_snap = snap
        return snap

    @contextlib.contextmanager
    def _window_scope(self):
        """Операция окна (снятие, закрытие разрыва, «забыть шаг»): головы
        и виджеты шагов считаются один раз — на операцию, а не на каждое
        обращение. Ленту внутри меняет только само окно: снятие шага
        правит снимок (`_Snapshot.forget`), монтаж куска его сбрасывает."""
        self._window_scoped += 1
        try:
            yield
        finally:
            self._window_scoped -= 1
            if not self._window_scoped:
                self._window_snap = None

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
        """Память эфемерного экрана: снятые шаги возвращаются с ней."""
        return None

    def _window_adopt_chunk(self, refs: ChunkRefs) -> None:
        """Кусок смонтирован: принять его ссылки (виды блоков)."""

    def _window_pinned(self, step: int) -> bool:
        """Шаг снимать нельзя (§5.3): снятие с края на нём
        останавливается."""
        return False

    def _window_focus_home(self) -> None:
        """Явный переход в конец: фокус со старой строки — домой (§5.11)."""
        self.set_focus(None)

    # ---- хвост и куски ---------------------------------------------------

    def _window_mount_tail(self, stop: int | None = None) -> ChunkRefs:
        """Новое окно: последние шаги на ``TAIL_MIN`` виджетов в конец
        ленты. Прежнее поколение окна больше не действует. ``stop`` —
        шаги от него рисует сам экран (незаконченный ход — живым путём);
        None — хвост до конца модели."""
        self._window_reset()
        model = self._window_model()
        estimates = self._window_estimates(model)[:stop]
        steps = policy.tail_steps(estimates, policy.TAIL_MIN)
        self._window_first = steps.start
        refs = self._window_render(steps, before=None)
        # Догрузка решает по высоте разложенного хвоста.
        self._window_feed().call_after_layout(self._window_fill)
        return refs

    def _window_reset(self) -> None:
        """Окно пусто — новая лента (смена сессии, /new, пересборка):
        прежнее поколение окна больше не действует, кусок прежнего окна
        выброшен; новая лента уже «у низа» — возврат к низу не ужимает
        старую."""
        self._window_gen += 1
        self._window_busy = False
        self._window_stuck = True
        self._window_trim_due = False
        self._window_grown = 0
        self._window_grow_step = -1
        self._window_step_of = WeakKeyDictionary()
        self._window_replies = {}
        self._window_reply_step = None
        self._window_first = 0
        self._window_gap = range(0)
        self._window_feed().hold_first_visible(None)

    def _window_render(
        self, steps: range, *, before: Widget | None
    ) -> ChunkRefs:
        refs = render_steps(
            self._window_host(),
            self._window_model(),
            steps,
            before=before,
            options=self._window_options(),
            ephemera=self._window_ephemera(),
            nested=self._window_nested(),
        )
        self._window_step_of.update(refs.step_of)
        self._window_snap = None
        return refs

    @staticmethod
    def _window_estimates(model: HistoryModel) -> list[int]:
        """Оценка виджетов каждого шага модели — до монтажа."""
        starts = model.step_starts
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
        steps = policy.prev_chunk(
            self._window_first,
            self._window_estimates(model),
            policy.CHUNK_MIN,
            policy.CHUNK_MAX,
        )
        if not steps:
            return False
        refs = self._window_insert(steps, before=self._window_top())
        self._window_first = steps.start
        self._window_settle_later(refs, "top", focus)
        return True

    def _window_append(self, *, focus: bool) -> bool:
        """Кусок перед разрывом (§5.11) — зеркально `_window_prepend`."""
        gap = self._window_gap
        steps = policy.next_chunk(
            gap.start,
            gap.stop,
            self._window_estimates(self._window_model()),
            policy.CHUNK_MIN,
            policy.CHUNK_MAX,
        )
        if not steps:
            return False
        refs = self._window_insert(steps, before=self._window_gap_head())
        self._window_set_gap(steps.stop, gap.stop)
        self._window_settle_later(refs, "bottom", focus)
        return True

    def _window_insert(
        self, steps: range, *, before: Widget | None
    ) -> ChunkRefs:
        """Шаги ``steps`` — перед ``before``; якорь держит читаемую
        строку."""
        self._window_feed().hold_first_visible(self._window_reading_row())
        refs = self._window_render(steps, before=before)
        self._window_adopt_chunk(refs)
        return refs

    def _window_settle_later(
        self, refs: ChunkRefs, side: Side, focus: bool
    ) -> None:
        """Кусок встаёт под удержанием отрисовки до монтажа; потом —
        `_window_settle` очередью ленты: после того, как лента разобрала
        сообщения монтажа куска. Иначе подъём кусок за куском обгоняет её
        очередь, а ждущие в ней сообщения держат снятые шаги."""
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
        self._window_reach_reply()
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
        """Остров: прочитанное не ужать — оба его конца закреплены.
        Снятие идёт только с концов (второго разрыва не бывает), поэтому
        разрыв под ним не закрыть, даже если между закреплёнными остались
        обычные ходы. Оба конца — по одному снимку шагов."""
        with self._window_scope():
            return self._window_pinned(self._window_first) and (
                self._window_pinned(self._window_gap.start - 1)
            )

    def _window_on_bottom(self) -> None:
        """Переход к низу — одна точка всех путей (отправка, команда,
        плашка, клавиша): разрыв открыт — фокус со старой строки домой и
        разрыв закрывается; нет — ужатие сверху до лимита целыми шагами с
        запасом против петли с домонтированием. Пока идёт выделение —
        нет; пока встаёт кусок — когда встанет (если лента всё ещё у
        низа)."""
        if self._ui_frozen_for_select():
            return
        self._window_reply_step = None
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
        tail = policy.tail_steps(estimates, policy.TAIL_MIN)
        low = gap.start if gap else 0
        if policy.short_gap(sum(estimates[low:stop])):
            tail = range(low, stop)
        steps = range(max(tail.start, low), stop)
        # Как при открытии: хвост ниже запаса ленты — догрузка после
        # раскладки.
        self._window_feed().call_after_layout(self._window_fill)
        if not steps:
            return
        before = self._window_gap_head() if gap else self._window_top()
        refs = self._window_insert(steps, before=before)
        if gap:
            self._window_set_gap(gap.start, steps.start)
        else:
            self._window_first = steps.start
        self._window_settle_later(refs, "end", False)

    def _window_forget_read_range(self) -> None:
        """Снять прочитанный диапазон с обоих концов, до закреплённых."""
        with self._window_scope():
            self._window_forget_read_range_now()

    def _window_forget_read_range_now(self) -> None:
        for edge in ("top", "bottom"):
            while self._window_gap:
                gap = self._window_gap
                step = self._window_first if edge == "top" else gap.start - 1
                if not self._window_forget_step(step):
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

    def _window_appended(self, step: int, widget: Widget) -> None:
        """Живой путь смонтировал ``widget`` шага ``step``: метка шага.
        Сверка с лимитом — не на каждый виджет (это пересчёт окна): с
        началом шага и после роста на кусок по оценке, после
        раскладки."""
        if step < 0:
            return
        self._window_step_of[widget] = step
        new_step = step > self._window_grow_step
        self._window_grow_step = max(step, self._window_grow_step)
        self._window_grown += policy.ITEM_WIDGETS
        if not new_step and self._window_grown < policy.CHUNK_MIN:
            return
        self._window_grown = 0
        if not self._window_grow_queued:
            self._window_grow_queued = True
            self._window_feed().call_after_layout(self._window_on_grown)

    def _window_step_at(self, widget: Widget) -> int | None:
        """Шаг, в котором ``widget`` лежит по ленте (от головы шага до
        следующей); выше первой головы или не в ленте — None. У помеченного
        виджета — его метка: якоря рядом со своим виджетом (строки блоков,
        вложенное) помечены шагом по ленте; проход по ленте — только для
        непомеченного (вложенное из куска)."""
        step = self._window_step_of.get(widget)
        if step is not None:
            return step
        step_at = {w: t for t, w in self._window_heads.items()}
        for child in self._window_feed().children:
            step = step_at.get(child, step)
            if child is widget:
                return step
        return None

    def _window_on_grown(self) -> None:
        """Лента выросла у низа: снять старые шаги сверх лимита — только
        пока она прилипла к низу, без выделения и пока кусок не встаёт.
        Рост живого хода при чтении снятия не запускает (§5.11): его
        проверит следующий кусок или переход в конец."""
        self._window_grow_queued = False
        if self._stick_bottom and not self._window_busy:
            self._window_trim("top")

    def _window_trim(self, edge: Edge) -> None:
        """Ужать окно до лимита целыми шагами с края ``edge`` (``top`` /
        ``bottom``), если оно больше «лимит + кусок» (запас против петли с
        домонтированием). Закреплённое считается в лимите; снимаются шаги
        от края до удерживаемых (видимое с запасами) или закреплённого."""
        if self._ui_frozen_for_select() or (
            # Низ снимается только при чтении: у низа там живой ход.
            edge == "bottom" and self._stick_bottom
        ):
            return
        if self._window_busy:
            return
        with self._window_scope():
            dropped = self._window_trim_now(edge)
        if dropped:
            self.call_after_refresh(self._window_fill)

    def _window_trim_now(self, edge: Edge) -> bool:
        """Тело `_window_trim` — внутри одной операции окна; True — шаги
        сняты."""
        self._window_skip_empty()
        steps, counts = self._window_mounted_counts()
        excess = policy.trim_to(
            sum(counts), policy.WINDOW_LIMIT, policy.CHUNK_MIN
        )
        if excess is None:
            return False
        kept = self._window_kept_steps()
        if kept is None:
            return False
        order = self._window_edge_order(edge, list(zip(steps, counts)))
        removable = list(
            takewhile(
                lambda m: m[0] not in kept and not self._window_pinned(m[0]),
                order,
            )
        )
        dropped = self._window_keep_filled(
            removable[: policy.edge_drops([c for _t, c in removable], excess)]
        )
        if not dropped:
            return False
        if edge == "top" and not self._stick_bottom:
            self._window_feed().hold_first_visible(self._window_reading_row())
        for step, _count in dropped:
            if not self._window_forget_step(step):
                break
        return True

    def _window_keep_filled(
        self, dropped: list[tuple[int, int]]
    ) -> list[tuple[int, int]]:
        """Снимаемые шаги (от края внутрь) без тех, после снятия которых
        лента станет ниже запаса ``PREFETCH_SCREENS``: иначе догрузка
        (`_window_fill`) тут же домонтирует их обратно — у низа это цикл
        «снять ↔ домонтировать» на каждый приход к низу. Высота шага — по
        последней раскладке; раскладки не было — как раньше (у низа без
        неё снимается всё, кроме последнего шага)."""
        feed = self._window_feed()
        if feed.placed_between(0, 1) is None:
            return dropped
        height = feed.container_size.height
        rows_of = self._window_rows_by_step()
        rows = [rows_of.get(step, 0) for step, _count in dropped]
        left = feed.virtual_size.height - sum(rows)
        while dropped and policy.underfilled(left - height, height):
            dropped = dropped[:-1]
            left += rows.pop()
        return dropped

    def _window_rows_by_step(self) -> dict[int, int]:
        """Строк ленты под каждым смонтированным шагом по последней
        раскладке — одним проходом: от верха первого показанного виджета
        шага до верха первого показанного виджета следующих шагов (или
        конца ленты); у шага без показанных — нет записи. Верх виджетов —
        одной картой раскладки."""
        feed = self._window_feed()
        tops = feed.placed_tops() or {}
        step_at = {w: t for t, w in self._window_heads.items()}
        rows: dict[int, int] = {}
        step = current = None
        top_of_current = 0
        for widget in feed.children:
            step = step_at.get(widget, step)
            if step is None or step == current:
                continue
            top = tops.get(widget)
            if top is None:
                continue
            if current is not None:
                rows[current] = max(0, top - top_of_current)
            current, top_of_current = step, top
        if current is not None:
            rows[current] = max(0, feed.virtual_size.height - top_of_current)
        return rows

    def _window_edge_order(
        self, edge: Edge, mounted: list[tuple[int, int]]
    ) -> list[tuple[int, int]]:
        """Смонтированные шаги от края ``edge`` внутрь прочитанного
        диапазона. Снизу: при разрыве — сначала незакреплённые шаги в
        начале закреплённого низа (переставшие быть живыми), потом низ
        прочитанного; без разрыва — мимо закреплённых шагов у низа."""
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

    def _window_kept_steps(self) -> set[int] | None:
        """Шаги, задевающие видимое с запасами (`policy.keep_rows`).
        Раскладки не было — None. У низа видимое — последний экран
        ленты: прокрутка к низу после роста может ещё не дойти (открытие
        рисует живой ход, потом встаёт к низу)."""
        feed = self._window_feed()
        stuck = self._stick_bottom
        rows = policy.keep_rows(
            feed.max_scroll_y if stuck else feed.scroll_y,
            feed.container_size.height,
            stuck=stuck,
        )
        widgets = feed.placed_between(*rows)
        if widgets is None:
            if not self._stick_bottom:
                return None
            widgets = set()
        step_at = {w: t for t, w in self._window_heads.items()}
        kept: set[int] = set()
        step = None
        for widget in feed.children:
            step = step_at.get(widget, step)
            if step is not None and widget in widgets:
                kept.add(step)
        if not kept and self._window_heads:
            # Удерживаемых нет — последний шаг остаётся, даже гигантский.
            kept.add(max(self._window_heads))
        return kept

    def _window_mounted_counts(self) -> tuple[list[int], list[int]]:
        """Смонтированные шаги с виджетами и число виджетов поддерева
        ленты каждого: шаг — от своего первого виджета до первого
        следующего."""
        step_at = {w: t for t, w in self._window_heads.items()}
        steps: list[int] = []
        counts: list[int] = []
        for widget in self._window_feed().children:
            step = step_at.get(widget)
            if step is not None:
                steps.append(step)
                counts.append(0)
            if counts:
                counts[-1] += len(widget.walk_children(with_self=True))
        return steps, counts

    def _window_removable(self, step: int) -> bool:
        """Шаг на конце прочитанного диапазона: верхний или нижний перед
        разрывом, либо первый за разрывом; без разрыва — любой
        смонтированный (снятие откроет разрыв на нём)."""
        gap = self._window_gap
        if step == self._window_first:
            return True
        if gap:
            return step in (gap.start - 1, gap.stop)
        return step in self._window_heads

    def _window_forget_step(self, step: int) -> bool:
        """Снять шаг на конце прочитанного диапазона целиком; не на конце
        — False."""
        if not self._window_removable(step):
            return False
        widgets = self._window_step_widgets(step)
        self._window_drop(step)
        self._window_feed().remove_children(widgets)
        if self._window_snap is not None:
            self._window_snap.forget(step)
        return True

    def _window_drop(self, step: int) -> None:
        """Границы окна без снятого шага ``step``. Прочитанный диапазон
        снят целиком — окно сходится к закреплённому низу."""
        gap = self._window_gap
        if step == self._window_first:
            self._window_first = step + 1
            if gap and self._window_first == gap.start:
                self._window_first, self._window_gap = gap.stop, range(0)
        elif not gap:
            self._window_gap = range(step, step + 1)
        elif step == gap.start - 1:
            self._window_gap = range(step, gap.stop)
        else:
            self._window_gap = range(gap.start, step + 1)
        self._window_skip_empty()

    def _window_skip_empty(self) -> None:
        """Границы прочитанного диапазона — по шагам с виджетами (R45).
        Шаг, чьи виджеты все сняты мимо окна (откат реплики до вывода
        агента, свёртка единственной попытки шага), головы не имеет: снятие
        его не видит, и «первым» или краем разрыва он остановил бы снятие
        навсегда. Верх окна проматывается через такие шаги (до последнего
        шага модели), нижний край прочитанного — вверх, верх закреплённого
        низа за разрывом — вниз (не на последний шаг: он закреплён и вот-вот
        получит виджеты); прочитанное без виджетов — окно сходится к
        закреплённому низу. Голов нет совсем (лента снимается целиком: смена
        сессии, закрытие экрана) — границы не трогаются: мерить не по чему.
        Это опирается на порядок в `_window_forget_step`: границы сдвигает
        `_window_drop` до `remove_children`, пока снятый шаг ещё в ленте и
        голов не ноль."""
        heads = self._window_heads
        if not heads:
            return
        last = self._window_model().step_count() - 1
        while True:
            gap = self._window_gap
            stop = gap.start if gap else last
            while (
                self._window_first < stop and self._window_first not in heads
            ):
                self._window_first += 1
            if not gap or self._window_first < gap.start:
                break
            self._window_first, self._window_gap = gap.stop, range(0)
        gap = self._window_gap
        while (
            gap
            and gap.start - 1 > self._window_first
            and (gap.start - 1 not in heads)
        ):
            gap = range(gap.start - 1, gap.stop)
        while gap and gap.stop < last and gap.stop not in heads:
            gap = range(gap.start, gap.stop + 1)
        self._window_gap = gap

    def _window_step_widgets(self, step: int) -> list[Widget]:
        """Виджеты ленты шага ``step``: от его первого виджета до первого
        виджета следующего смонтированного шага. Шаг без виджетов —
        пусто."""
        return list(self._window_shot().widgets.get(step, ()))

    # ---- липкая реплика (R46) --------------------------------------------

    def _sticky_reply(self, placements, scroll_y: int) -> Widget | None:
        """Реплика липкой строки (R46): смонтированная ближайшая выше края
        — как раньше, если она из хода читаемой строки; нет такой, а шаг
        реплики этого хода снят окном (чтение посреди гигантского хода), —
        реплика хода из модели тем же видом. Шаг строки и реплики — по
        меткам, без прохода по ленте."""
        mounted = super()._sticky_reply(placements, scroll_y)
        step = self._window_step_in(placements, scroll_y)
        if step is None:
            return mounted
        model = self._window_model()
        turn = model.step_turn(step)
        own = None if mounted is None else self._window_step_of.get(mounted)
        if (own is not None and model.step_turn(own) == turn) or (
            mounted is not None and own is None
        ):
            return mounted
        if self._window_mounted(model.turn_step(turn)):
            return mounted
        return self._window_turn_reply(turn) or mounted

    def _window_step_in(
        self, placements: Sequence, scroll_y: int
    ) -> int | None:
        """Шаг строки ленты на ``scroll_y``: метка ближайшего помеченного
        виджета на ней или выше (раскладка ``placements`` — по порядку
        ленты)."""
        at = bisect_right(placements, scroll_y, key=lambda p: p.region.y)
        for i in range(at - 1, -1, -1):
            step = self._window_step_of.get(placements[i].widget)
            if step is not None:
                return step
        return None

    def _window_mounted(self, step: int) -> bool:
        """Шаг в смонтированном диапазоне окна (вне разрыва)."""
        return step >= self._window_first and step not in self._window_gap

    def _window_turn_reply(self, turn: int) -> Widget | None:
        """Реплика хода ``turn`` из модели — виджет вне ленты (как его
        рисует кусок); у хода без показанной реплики — None."""
        model = self._window_model()
        if turn >= model.turn_count():
            return None
        ev = model.items[model.turn_starts[turn]].event
        if ev is None or ev.kind != "user" or not shown(ev, model.withdrawn):
            return None
        reply = self._window_replies.get(ev.no)
        if reply is None:
            reply = self._window_replies[ev.no] = reply_widget(ev)
        return reply

    def on_scroll_to_reply(self, message: ScrollToReply) -> None:
        """Клик по липкой реплике из модели: шаги до реплики встают
        обычными кусками окна, как подъём (`_window_reach_reply`),
        реплика — наверху. Смонтированная — как раньше."""
        reply = message.reply
        if reply.is_attached:
            self._window_reply_step = None
            super().on_scroll_to_reply(message)
            return
        no = getattr(reply, "event_no", 0)
        if self._window_replies.get(no) is not reply:
            return
        model = self._window_model()
        self._window_reply_step = model.turn_step(model.turn_index_of(no))
        self._window_reach_reply()

    def _window_reach_reply(self) -> None:
        """Шаг к реплике клика (R47): шаг реплики смонтирован — она
        наверху, путь окончен; нет — лента к краю смонтированного и
        кусок к нему (не больше ``CHUNK_MAX``), противоположный край
        снимается, как при подъёме. Следующий шаг — когда кусок встанет
        (`_window_settle`)."""
        step = self._window_reply_step
        if step is None or self._window_busy:
            return
        if self._window_mounted(step):
            self._window_reply_step = None
            self._window_feed().call_after_layout(
                lambda: self._window_show_head(step)
            )
            return
        if step < self._window_first:
            self._window_reply_scroll(0)
            done = self._window_prepend(focus=False)
        else:
            done = self._window_reach_gap_bottom(step)
        if not done:
            self._window_reply_step = None

    def _window_reply_scroll(self, y: float) -> None:
        """Прокрутка ленты самим путём к реплике — запомнена, чтобы не
        принять её за уход человека (`_on_trace_scrolled`)."""
        feed = self._window_feed()
        y = min(y, feed.max_scroll_y)
        self._window_reply_y = round(y)
        feed.scroll_to(y=y, animate=False, immediate=True)

    def _window_reach_gap_bottom(self, step: int) -> bool:
        """Шаг реплики в разрыве — у его нижнего края, над закреплённым
        низом (читаемое за разрывом, ход начат в разрыве): кусок, который
        кончается на конце разрыва, — перед закреплённым низом, разрыв
        сужается снизу; зеркально подъёму, не выходя за ход реплики.
        Лента — к верху закреплённого низа."""
        gap = self._window_gap
        prev = policy.prev_chunk(
            gap.stop,
            self._window_estimates(self._window_model()),
            policy.CHUNK_MIN,
            policy.CHUNK_MAX,
        )
        steps = range(max(prev.start, gap.start, step), gap.stop)
        if not steps:
            return False
        gap_y = self._window_gap_y()
        if gap_y is not None:
            self._window_reply_scroll(gap_y)
        refs = self._window_insert(steps, before=self._window_gap_head())
        self._window_set_gap(gap.start, steps.start)
        self._window_settle_later(refs, "bottom", False)
        return True

    def _window_show_head(self, step: int) -> None:
        """Первый виджет шага ``step`` — наверху ленты."""
        head = self._window_heads.get(step)
        if head is not None:
            self._window_feed().scroll_to_widget(head, animate=False, top=True)

    # ---- отклик на прокрутку ---------------------------------------------

    def _on_trace_scrolled(self, value: float) -> None:
        """Прокрутка пользователем: удерживать первую видимую строку,
        пока лента не у низа, и домонтировать к краю, если пора."""
        super()._on_trace_scrolled(value)
        if self._ui_frozen_for_select():
            return
        if self._window_reply_step is not None and (
            round(value) != self._window_reply_y
        ):
            # Человек ушёл прокруткой сам — путь к реплике не перехватывает.
            self._window_reply_step = None
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
