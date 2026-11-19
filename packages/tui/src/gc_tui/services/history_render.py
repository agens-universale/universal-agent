"""Отрисовщик кусков истории: шаги ходов модели истории — в ленту (спека
шага 2 окна ленты, §5.2).

Кусок — подряд идущие шаги (`HistoryModel.step_starts`); ход, нарисованный
по шагам, выглядит так же, как целиком: у каждого шага свой вид блоков —
граница шага закрывает группу вызовов, и живой путь закрывает её там же
(`trace_steps`), а состояние хода, которое шаг получает от предыдущих
(дедуп ответа), отрисовщик проигрывает по модели тем же правилом
(`replay_answer`), если кусок начинается внутри хода; попытки сдачи,
которые свернутся, модель помечает заранее — шаг их не рисует. Итог хода
и обрыв вызовов без исхода — всем шагам хода в куске.

Без побочных эффектов на экран: состояние куска (вид блоков хода, дедуп
ответа, вызовы без исхода) живёт в самом проходе, а
ссылки, которые нужны живому состоянию экрана, возвращаются в
``ChunkRefs``. Им же рисует превью `/sessions`: история в превью и в
сессии — один код.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from gc_core.config_modules.agent_edits import (
    DEFAULT_REPLAY_EXPANDED_TURNS,
    DEFAULT_TRACE_DIFF_PREVIEW_LINES,
)
from gc_core.persistence.model_choice_record import parse_model_choice
from gc_core.persistence.turn_outcome import parse_turn_outcome

from gc_tui.services.answer_flow import (
    AnswerFlow,
    answer_verdict,
    finish_close,
    replay_answer,
)
from gc_tui.services.think_text import think_rejected
from gc_tui.services.tool_blocks import CallTimers, ToolBlocks
from gc_tui.services.trace_ephemera import EPHEMERAL_CLASS, anchor_key
from gc_tui.services.trace_mapper import PROPOSE_PLAN_ID, is_finish
from gc_tui.services.trace_render import (
    QUESTION_UNANSWERED,
    TAG_RE,
    build_agent_widgets,
    build_answered_question_widget,
    build_block_widgets,
    build_call_row,
    build_compaction_widgets,
    build_finish_widgets,
    build_intermediate_widgets,
    build_orphan_result_row,
    build_standalone_row,
    build_turn_line,
    note_question,
    result_with_edits,
    tool_caption,
)
from gc_tui.services.trace_steps import RESULT_KINDS, shown
from gc_tui.services.turn_outcome import outcome_error
from gc_tui.textual_compat import is_being_removed
from gc_tui.widgets.model_mark import (
    ModelMark,
    drop_mark_before,
    restored_mark,
)
from gc_tui.widgets.thought_row import ThoughtRow
from gc_tui.widgets.tool_blocks_view import (
    BlockViews,
    ToolBlocksView,
    diff_view_key,
    end_turn,
)

if TYPE_CHECKING:
    from gc_core.constants.tool_display import McpToolInfo
    from textual.widget import Widget

    from gc_tui.models import TraceEvent
    from gc_tui.services.history_model import HistoryItem, HistoryModel
    from gc_tui.services.trace_ephemera import Ephemera
    from gc_tui.widgets.tool_call_row import ToolCallRow

#: Пространство имён уровней блоков кусков: не пересекается ни с уровнем
#: живой ленты (``""``), ни с уровнями вложенных блоков (call_id родителя).
HISTORY_LEVEL_PREFIX = "\x00history:"


class HistoryHost(Protocol):
    """Лента, в которую рисуется кусок (лента — параметр: экран сессии,
    превью, лента экрана субагента)."""

    feed: Widget

    def mount(
        self,
        *widgets: Widget,
        before: Widget | None = None,
        after: Widget | None = None,
    ) -> None: ...


@dataclass
class FeedHost:
    """Хост-лента без своих правил монтажа (превью, тесты)."""

    feed: Widget

    def mount(self, *widgets, before=None, after=None) -> None:
        if before is not None:
            self.feed.mount(*widgets, before=before)
        elif after is not None:
            self.feed.mount(*widgets, after=after)
        else:
            self.feed.mount(*widgets)


@dataclass
class _FlushingHost:
    """Хост видов блоков куска: их виджеты встают в ленту сразу (виды
    ставят соседей по ним, держат экран до их монтажа)."""

    chunk: _Chunk

    def mount(self, widget, *, before=None, after=None) -> None:
        self.chunk.mount(widget, before=before, after=after)
        self.chunk.flush()


class NestedSource(Protocol):
    """Узел вложенного трейса (§5.9): вложенное для родителей куска и его
    домонтирование. Что из вызовов куска родитель, решает узел."""

    def actions_for(self, parent_ids: set[str]) -> list: ...

    def mount(
        self,
        host: HistoryHost,
        actions: list,
        calls: dict[str, Widget],
        *,
        views: BlockViews,
        options: ChunkOptions,
    ) -> list[Widget]: ...


@dataclass
class ChunkOptions:
    """Параметры хоста. ``focusable_compaction`` — метка сжатия ловит
    фокус (в превью нет: Enter там открывает сессию). ``timers`` — реестр
    застывших таймеров вызовов хоста; ``plan_accepted`` — состояние рамки
    плана при вставке (решение человека принадлежит экрану);
    ``thoughts`` — строки мыслей из памяти эфемерного показаны (флаг
    показа мыслей сейчас); ``fresh_line`` — у хоста есть итог «сейчас»:
    строка итога последнего хода свежая (в превью нет — как все)."""

    focusable_compaction: bool = False
    edits_lookup: Any = None
    diff_preview: int = DEFAULT_TRACE_DIFF_PREVIEW_LINES
    expanded_turns: int = DEFAULT_REPLAY_EXPANDED_TURNS
    mcp_tools: Mapping[str, McpToolInfo] | None = None
    mcp_servers: Iterable[str] = ()
    model_slots: Mapping[str, str | None] = field(default_factory=dict)
    timers: CallTimers = field(default_factory=CallTimers)
    plan_accepted: Callable[[str], bool] | None = None
    thoughts: bool = True
    fresh_line: bool = True


@dataclass
class ChunkRefs:
    """Что кусок отдаёт экрану. ``widgets`` — прямые дети ленты, которые
    смонтировал кусок; ``step_of`` — шаг каждого из них (вложенное без
    метки — лежит внутри шага): по меткам окно находит головы шагов
    (`trace_window.step_heads`); ``block_views`` — виды блоков шагов
    куска (по одному на шаг с вызовами, уровень в пространстве
    ``HISTORY_LEVEL_PREFIX``) — только для маршрута раскрытия, не в живой
    реестр видов; ``thoughts`` — строки мыслей из памяти эфемерного;
    ``attempts`` — виджеты попыток сдачи последнего хода, которые ещё
    может свернуть следующая (экран живого хода снимет их при свёртке)."""

    widgets: list[Widget] = field(default_factory=list)
    step_of: dict[Widget, int] = field(default_factory=dict)
    fresh_turn_line: Widget | None = None
    call_rows: dict[str, Widget] = field(default_factory=dict)
    question_rows: dict[str, Widget] = field(default_factory=dict)
    summary_markers: dict[int, Widget] = field(default_factory=dict)
    summary_bodies: dict[int, Widget] = field(default_factory=dict)
    block_views: BlockViews = field(default_factory=BlockViews)
    thoughts: list[Widget] = field(default_factory=list)
    attempts: list[list[Widget]] = field(default_factory=list)


def render_steps(
    host: HistoryHost,
    model: HistoryModel,
    steps: range,
    *,
    before: Widget | None,
    options: ChunkOptions,
    ephemera: Ephemera | None = None,
    nested: NestedSource | None = None,
) -> ChunkRefs:
    """Шаги ``steps`` модели — в ленту перед ``before`` (None — в конец).
    Черта смены модели на стыке с куском ниже схлопывается с ним так же,
    как подряд. ``nested`` — вложенный трейс под вызовами куска;
    ``ephemera`` — снятое эфемерное и снимок вида шагов куска (§5.4)."""
    chunk = _Chunk(host, model, before, options, ephemera)
    for s in steps:
        chunk.step(s)
    chunk.end_turn_part()
    if nested is not None:
        chunk.nest(nested)
    return chunk.finish()


def reply_widget(ev: TraceEvent) -> Widget:
    """Виджет реплики события ``ev`` — как его рисует кусок; им же окно
    показывает липкую реплику хода, чей шаг снят."""
    from gc_tui.widgets.user_reply import UserReplyContainer

    return UserReplyContainer(
        ev.summary,
        event_no=ev.no,
        images=ev.attachments,
        skill=ev.skill or None,
    )


def history_level(step: int) -> str:
    """Уровень блоков шага ``step`` куска."""
    return f"{HISTORY_LEVEL_PREFIX}{step}"


class _Chunk:
    """Один проход отрисовки куска: состояние только своё.

    Свои виджеты в позицию куска (без якоря) кусок копит и отдаёт ленте
    пачкой: каждый вызов `Widget.mount` кладёт в очередь ленты снимок всех
    её детей, и поштучный монтаж при подъёме копит их быстрее, чем лента
    разбирает, — снимки держат снятые виджеты. Всё, что смотрит на ленту
    как она есть (якорь, соседи, вложенное, удаление, пометки и вид
    виджетов), сначала сбрасывает пачку — `flush`; виды блоков получают
    хост, который сбрасывает её на каждый свой монтаж."""

    def __init__(
        self,
        host: HistoryHost,
        model: HistoryModel,
        before: Widget | None,
        options: ChunkOptions,
        ephemera: Ephemera | None = None,
    ) -> None:
        self._host = host
        self._ephemera = ephemera
        self._model = model
        self._before = before
        self._opts = options
        self.refs = ChunkRefs()
        self._mounted: list[Widget] = []
        # Виджеты позиции куска, ещё не отданные ленте (`flush`).
        self._pending: list[Widget] = []
        self._step_of: dict[Widget, int] = {}
        self._view: ToolBlocksView | None = None
        self._step: int | None = None
        self._turn: int | None = None
        # Нарисованная часть хода ``_turn``: первый её виджет в
        # ``_mounted``, виды блоков её шагов и последний её элемент модели.
        self._turn_from = 0
        self._turn_views: list[ToolBlocksView] = []
        self._turn_stop = 0
        self._flow = AnswerFlow()
        # Вызовы без исхода: строка и вид её хода.
        self._open: dict[str, tuple[ToolCallRow, ToolBlocksView]] = {}
        self._finish_ids: set[str] = set()
        self._ended = False
        self._fresh_at = (
            _fresh_outcome_index(model) if options.fresh_line else None
        )
        # Первое вложенное в ``_mounted`` (`nest`); вложенного нет — None.
        self._nested_from: int | None = None

    # ---- монтаж ----------------------------------------------------------

    def mount(self, widget, *, before=None, after=None) -> None:
        """Хост блоков и событий куска: без якоря — перед ``before``
        куска; событие не из блоков закрывает открытую группу."""
        if before is None and after is None:
            self._pending.append(widget)
        else:
            self.flush()
            self._host.mount(widget, before=before, after=after)
        self._mounted.append(widget)
        if self._step is not None:
            self._step_of[widget] = self._step
        view = self._view
        if view is not None and not view.issued(widget):
            view.event_closing()

    def flush(self) -> None:
        """Накопленное — в ленту одним вызовом перед ``before`` куска."""
        pending, self._pending = self._pending, []
        if pending:
            self._host.mount(*pending, before=self._before)

    def _mount_all(self, widgets: Iterable[Widget]) -> None:
        for widget in widgets:
            self.mount(widget)

    def _step_view(self) -> ToolBlocksView:
        """Вид блоков шага: один на шаг, создаётся первым вызовом шага.
        Группа вызовов не переходит через границу шага (`trace_steps`),
        поэтому вид на шаг рисует то же, что вид на весь ход."""
        level = history_level(self._step or 0)
        view = self.refs.block_views.get(level)
        if view is None:
            opts = self._opts
            model = ToolBlocks(opts.timers, diff_preview=opts.diff_preview)
            view = ToolBlocksView(
                _FlushingHost(self), model, level=level, depth=0
            )
            self.refs.block_views[level] = view
            self._turn_views.append(view)
            self._view = view
        return view

    # ---- проход ----------------------------------------------------------

    def step(self, s: int) -> None:
        model = self._model
        turn = model.step_turn(s)
        span = model.step_range(s)
        if turn != self._turn:
            self.end_turn_part()
            self._begin_turn(turn, span.start)
        self._step = s
        rows = self._ephemera.for_step(model, s) if self._ephemera else []
        for k in span:
            if not self._settled_early(k):
                self._item(k, model.items[k])
            rows = self._mount_ephemeral(rows, k + 1)
        self._mount_ephemeral(rows, None)
        self._settle_calls(s, span.stop)
        self._turn_stop = span.stop

    def _turn_end(self) -> int:
        """Конец хода ``_turn`` в ``items``."""
        model = self._model
        turn = (self._turn or 0) + 1
        if turn < model.turn_count():
            return model.turn_starts[turn]
        return len(model.items)

    def _settle_calls(self, s: int, start: int) -> None:
        """Исходы вызовов шага ``s``, пришедшие в следующих шагах хода
        (с ``start``), — сейчас: их виджеты встают якорем к строке вызова,
        и шаг выглядит так же, как в ходе целиком (`trace_steps`)."""
        model = self._model
        stop = self._turn_end()
        for call_id in list(self._open):
            at = model.first_result(call_id)
            call = model.call_item(call_id)
            if at is None or call is None or not start <= at < stop:
                continue
            if model.item_step(call) == s:
                self._event(model.items[at].event)

    def _settled_early(self, k: int) -> bool:
        """Элемент ``k`` — исход вызова из прошлого шага того же хода: он
        уже применён в конце шага вызова (`_settle_calls`)."""
        model = self._model
        ev = model.items[k].event
        if ev is None or ev.kind not in RESULT_KINDS:
            return False
        if model.first_result(ev.call_id) != k:
            return False
        call = model.call_item(ev.call_id)
        if call is None:
            return False
        step = model.item_step(k)
        return model.item_step(call) != step and model.step_turn(
            model.item_step(call)
        ) == model.step_turn(step)

    def _begin_turn(self, turn: int, start: int) -> None:
        """Часть хода ``turn`` с элемента ``start``: кусок, начатый внутри
        хода, проигрывает дедуп ответа по элементам хода до ``start`` —
        шаг рисует прозу так же, как при отрисовке хода целиком."""
        model = self._model
        self._turn = turn
        self._flow = AnswerFlow()
        self._ended = turn + 1 < model.turn_count()
        self._turn_from = len(self._mounted)
        self._turn_views = []
        for item in model.items[model.turn_starts[turn] : start]:
            if item.kind == "event" and shown(item.event, model.withdrawn):
                replay_answer(self._flow, item.event)

    def end_turn_part(self) -> None:
        """Нарисованная часть хода кончилась. Итог хода за ней (в шагах
        вне куска) — видам блоков её шагов, как если бы ход шёл до него;
        ход кончился — вызовы без исхода оборвались; потом снимок вида."""
        if self._turn is None:
            return
        for outcome in self._outcomes_after(self._turn_stop):
            self._end_turn_blocks(outcome)
        if self._ended:
            self._interrupt_open()
        if self._ephemera is not None:
            # Снимок вида — пока ход свой: смонтированное раскрытием (фразы
            # группы) — виджеты этого хода, а не последнего в куске.
            self._restore_view(
                self._ephemera,
                self._mounted[self._turn_from :],
                self._turn_views,
            )
        self._turn = self._step = None

    def _outcomes_after(self, start: int) -> list:
        """Записи итога хода ``_turn`` с элемента ``start`` — те, что
        нарисовали бы строку итога."""
        model = self._model
        out = []
        for item in model.items[start : self._turn_end()]:
            if item.kind != "outcome":
                continue
            outcome = parse_turn_outcome(item.side)
            if (
                outcome is not None
                and outcome.opened_by not in model.withdrawn
            ):
                out.append(outcome)
        return out

    def _end_turn_blocks(self, outcome) -> None:
        """Итог хода — видам блоков его шагов в куске."""
        end_turn(
            self.refs.block_views,
            self._opts.timers,
            outcome,
            settled=True,
            levels=self._turn_views,
        )
        self._ended = True

    def _mount_ephemeral(self, rows: list, at: int | None) -> list:
        """Снятое эфемерное за элементом ``at - 1`` (None — всё
        оставшееся); мысли — по нынешнему флагу показа."""
        while rows and (at is None or rows[0][0] <= at):
            widget = rows.pop(0)[1]
            widget.add_class(EPHEMERAL_CLASS)
            if isinstance(widget, ThoughtRow):
                widget.display = self._opts.thoughts
                self.refs.thoughts.append(widget)
            self.mount(widget)
        return rows

    def _interrupt_open(self) -> None:
        """Ход кончился (есть итог или следующий ход): вызовы без исхода
        оборвались — как вживую на тике без живой задачи. Вызовы
        последнего хода без итога решает экран."""
        for row, view in self._open.values():
            view.interrupted(row, started=not row.waiting)
        self._open.clear()

    def _item(self, index: int, item: HistoryItem) -> None:
        if item.kind == "model_choice":
            self._model_mark(item.side)
        elif item.kind == "outcome":
            self._outcome(index, item.side)
        elif item.event is not None:
            self._event(item.event)

    def nest(self, nested: NestedSource) -> None:
        """Вложенное для вызовов куска — под их строками."""
        self.flush()
        calls = self.refs.call_rows
        self._turn = self._step = None
        self._nested_from = len(self._mounted)
        self._mounted += nested.mount(
            self._host,
            nested.actions_for(set(calls)),
            calls,
            views=self.refs.block_views,
            options=self._opts,
        )

    def finish(self) -> ChunkRefs:
        self.flush()
        if self._ephemera is not None and self._nested_from is not None:
            nested = [
                view
                for level, view in self.refs.block_views.items()
                if not level.startswith(HISTORY_LEVEL_PREFIX)
            ]
            mounted = self._mounted[self._nested_from :]
            self._restore_view(self._ephemera, mounted, nested)
        below = self._before
        if isinstance(below, ModelMark) and not is_being_removed(below):
            drop_mark_before(below)
        refs = self.refs
        refs.widgets = [w for w in self._mounted if not is_being_removed(w)]
        refs.step_of = self._step_of
        return refs

    def _restore_view(
        self,
        ephemera: Ephemera,
        widgets: list[Widget],
        views: list[ToolBlocksView],
    ) -> None:
        """Пометки снятых владельцев и снимок вида — виджетам и видам
        блоков (хода или вложенного трейса)."""
        self.flush()
        for widget in widgets:
            for body in ephemera.restore_decorations(
                anchor_key(widget), widget
            ):
                widget.add_consequence(body)
            view_key = getattr(widget, "view_key", None)
            flag = ephemera.view(view_key()) if view_key else None
            if flag is not None:
                widget.restore_view(flag)
        for view in views:
            view.restore_view(ephemera.view)

    # ---- записи сбоку ----------------------------------------------------

    def _model_mark(self, rec: dict) -> None:
        record = parse_model_choice(rec)
        if record is None:
            return
        mark = restored_mark(record, self._opts.model_slots)
        self.mount(mark)
        self.flush()
        drop_mark_before(mark)

    def _outcome(self, index: int, rec: dict) -> None:
        """Строка итога хода: свежая — только итог последнего хода, пока
        не началась реплика; время вызовов хода — застывшим таймерам."""
        outcome = parse_turn_outcome(rec)
        if outcome is None or outcome.opened_by in self._model.withdrawn:
            return
        self._end_turn_blocks(outcome)
        line = build_turn_line(outcome)
        line.freshen(None, outcome_error(outcome))
        if index == self._fresh_at:
            self.refs.fresh_turn_line = line
        else:
            line.settle()
        self.mount(line)

    # ---- события ---------------------------------------------------------

    def _event(self, ev: TraceEvent) -> None:
        if ev.hidden or ev.message_id in self._model.withdrawn:
            return
        handler = _HANDLERS.get(ev.kind, _Chunk._meta)
        handler(self, ev)

    def _user(self, ev: TraceEvent) -> None:
        """Реплика (и ответ на вопрос агента): дедуп ответа и попытки
        сбрасываются."""
        replay_answer(self._flow, ev)
        self.mount(reply_widget(ev))

    def _agent(self, ev: TraceEvent) -> None:
        if is_finish(ev):
            self._finish(ev)
            return
        if not answer_verdict(self._flow, ev).show:
            return
        if ev.tool_name == "intermediate_reply":
            self._mount_all(
                build_intermediate_widgets(ev.summary, event_no=ev.no)
            )
        elif ev.tool_name == PROPOSE_PLAN_ID:
            self._plan(ev)
        else:
            self._mount_all(build_agent_widgets(ev.summary, event_no=ev.no))

    def _plan(self, ev: TraceEvent) -> None:
        from gc_tui.widgets.plan_panel import PlanPanel

        widgets = build_block_widgets(
            ev.summary, prose_bullet=False, event_no=ev.no
        )
        panel = PlanPanel(*widgets, plan_text=ev.summary)
        accepted = self._opts.plan_accepted
        panel.set_accepted(bool(accepted and accepted(ev.summary)))
        self.mount(panel)

    def _finish(self, ev: TraceEvent) -> None:
        """Конец хода turn_finish: отчёт без повтора прозы. Попытку,
        которую свернёт следующая попытка или проза, повторившая отчёт,
        модель пометила (`HistoryModel.collapsed_attempts`, R42) — она не
        рисуется, в каком бы шаге ни лежала свёртка."""
        close = finish_close(self._flow, ev)
        if ev.call_id:
            self._finish_ids.add(ev.call_id)
        if ev.no in self._model.collapsed_attempts:
            return
        widgets = build_finish_widgets(close, event_no=ev.no)
        self._mount_all(widgets)
        if ev.call_id and self._turn == self._model.turn_count() - 1:
            # Её ещё может свернуть следующая попытка: экран живого хода
            # берёт её в свёртку (R44).
            self.refs.attempts.append(widgets)

    def _question(self, ev: TraceEvent) -> None:
        """Вопрос агента — видимая строка истории без баннера; «без
        ответа» — по модели."""
        question = build_answered_question_widget(
            ev.summary, event_no=ev.no, call_id=ev.call_id
        )
        if ev.call_id:
            self.refs.question_rows[ev.call_id] = question
            if ev.call_id in self._model.unanswered:
                note_question(question, QUESTION_UNANSWERED)
        self.mount(question)

    def _todo(self, ev: TraceEvent) -> None:
        from gc_core.execution.progress import todo_items_from_arguments

        from gc_tui.services.todo_render import build_todo_block

        items = todo_items_from_arguments(
            (ev.full_payload or {}).get("arguments")
        )
        self.mount(
            build_todo_block(items, subagent=False, indent="", event_no=ev.no)
        )

    def _call(self, ev: TraceEvent) -> None:
        opts = self._opts
        wire_name = ev.wire_name or ev.tool_name or "<tool>"
        row = build_call_row(
            call_id=ev.call_id or f"_anon_{ev.no}",
            wire_name=wire_name,
            args_summary=ev.args_summary,
            level=0,
            event_no=ev.no,
            mcp_tools=opts.mcp_tools,
            mcp_servers=opts.mcp_servers,
            agent=ev.agent,
            think_text=ev.think_text,
        )
        view = self._step_view()
        self.refs.call_rows[row.call_id] = row
        self._open[row.call_id] = (row, view)
        category, _ = tool_caption(wire_name, opts.mcp_tools, opts.mcp_servers)
        view.add_call(row, category, waiting=False, command=ev.command_lines)

    def _result(self, ev: TraceEvent) -> None:
        if ev.call_id in self._finish_ids:
            self._finish_ids.discard(ev.call_id)
            return
        opened = self._open.pop(ev.call_id, None) if ev.call_id else None
        if opened is not None:
            row, view = opened
            ephemera = self._ephemera
            result_with_edits(
                view,
                row,
                ev,
                edits_lookup=self._opts.edits_lookup,
                window=self._opts.expanded_turns,
                body=bool(
                    ephemera and ephemera.view(diff_view_key(row.call_id))
                ),
            )
        elif think_rejected((ev.full_payload or {}).get("output")):
            return  # отказ пустого Think: строки вызова нет
        elif ev.kind == "error":
            self.mount(
                build_orphan_result_row(
                    ev.summary, ev.result_outcome, event_no=ev.no
                )
            )
        else:
            self._row("   ", ev.summary, "line-ok", ev.no)

    def _summary(self, ev: TraceEvent) -> None:
        marker, body = build_compaction_widgets(
            ev, focusable=self._opts.focusable_compaction
        )
        self.mount(marker)
        self.refs.summary_markers[ev.no] = marker
        if body is not None:
            self.mount(body)
            self.refs.summary_bodies[ev.no] = body

    def _error_msg(self, ev: TraceEvent) -> None:
        """Ошибка с известным call_id закрывает свою строку вызова (схемные
        ошибки приходят техническим сообщением, не выводом вызова)."""
        opened = self._open.pop(ev.call_id, None) if ev.call_id else None
        if opened is not None:
            row, view = opened
            view.result(row, replace(ev, outcome="error"), None)
            return
        self.mount(
            build_standalone_row(ev.summary, severity="error", event_no=ev.no)
        )

    def _meta(self, ev: TraceEvent) -> None:
        if ev.tool_name == "ask_human" and self._question_shown(ev.call_id):
            return
        summary = ev.summary or ""
        m = TAG_RE.match(summary)
        if m:
            self._row(
                f"[{m.group('tag')}] ", m.group("rest"), "line-meta", ev.no
            )
        else:
            self._row("", summary, "line-meta", ev.no)

    def _question_shown(self, call_id: str | None) -> bool:
        """«Без ответа» вопроса уже стоит под его строкой (её рисует
        вопрос по модели). Вопроса в истории нет (ушёл в сжатие) —
        событие рисуется строкой."""
        return self._model.has_question(call_id)

    def _row(self, prefix: str, content: str, kind: str, no: int) -> None:
        from gc_tui.widgets.trace_row import TraceRow

        self.mount(
            TraceRow(
                prefix=prefix, content=content, kind_class=kind, event_no=no
            )
        )


_HANDLERS: dict[str, Callable[[_Chunk, TraceEvent], None]] = {
    "user": _Chunk._user,
    "agent": _Chunk._agent,
    "ask_human": _Chunk._question,
    "todo": _Chunk._todo,
    "call": _Chunk._call,
    "result": _Chunk._result,
    "error": _Chunk._result,
    "summary": _Chunk._summary,
    "error_msg": _Chunk._error_msg,
}


def _fresh_outcome_index(model: HistoryModel) -> int | None:
    """Индекс элемента записи итога, чья строка свежая: итог последнего
    хода, если после него не началась реплика."""
    if model.last_turn_outcome is None or model.outcome_superseded:
        return None
    return model.last_outcome_index()
