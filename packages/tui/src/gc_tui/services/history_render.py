"""Отрисовщик кусков истории: ходы модели истории целиком — в ленту (спека
шага 2 окна ленты, §5.2).

Без побочных эффектов на экран: состояние куска (вид блоков хода, дедуп
ответа, свёртка попыток, вызовы без исхода) живёт в самом проходе, а
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

from gc_tui.services.answer_flow import AnswerFlow
from gc_tui.services.think_text import think_rejected
from gc_tui.services.tool_blocks import CallTimers, ToolBlocks
from gc_tui.services.trace_ephemera import EPHEMERAL_CLASS, anchor_key
from gc_tui.services.trace_mapper import PROPOSE_PLAN_ID
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
    finish_close_of,
    note_question,
    result_with_edits,
    tool_caption,
)
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
        widget: Widget,
        *,
        before: Widget | None = None,
        after: Widget | None = None,
    ) -> None: ...


@dataclass
class FeedHost:
    """Хост-лента без своих правил монтажа (превью, тесты)."""

    feed: Widget

    def mount(self, widget, *, before=None, after=None) -> None:
        if before is not None:
            self.feed.mount(widget, before=before)
        elif after is not None:
            self.feed.mount(widget, after=after)
        else:
            self.feed.mount(widget)


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
    показа мыслей сейчас)."""

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


@dataclass
class ChunkRefs:
    """Что кусок отдаёт экрану. ``widgets`` — прямые дети ленты, которые
    смонтировал кусок; ``turn_heads`` — первый в ленте виджет каждого хода
    куска (ход без виджетов — без записи; вложенное лежит внутри хода):
    по ним окно режет ленту на ходы; ``block_views`` — виды блоков ходов
    куска (по одному на ход, уровень в пространстве
    ``HISTORY_LEVEL_PREFIX``) — только для маршрута раскрытия, не в живой
    реестр видов; ``thoughts`` — строки мыслей из памяти эфемерного."""

    widgets: list[Widget] = field(default_factory=list)
    turn_heads: dict[int, Widget] = field(default_factory=dict)
    fresh_turn_line: Widget | None = None
    call_rows: dict[str, Widget] = field(default_factory=dict)
    question_rows: dict[str, Widget] = field(default_factory=dict)
    last_model_mark: Widget | None = None
    summary_markers: dict[int, Widget] = field(default_factory=dict)
    summary_bodies: dict[int, Widget] = field(default_factory=dict)
    block_views: BlockViews = field(default_factory=BlockViews)
    thoughts: list[Widget] = field(default_factory=list)


def render_turns(
    host: HistoryHost,
    model: HistoryModel,
    turns: range,
    *,
    before: Widget | None,
    options: ChunkOptions,
    ephemera: Ephemera | None = None,
    nested: NestedSource | None = None,
) -> ChunkRefs:
    """Ходы ``turns`` модели целиком — в ленту перед ``before`` (None — в
    конец). Черта смены модели на стыке с куском ниже схлопывается с ним
    так же, как подряд. ``nested`` — вложенный трейс под вызовами куска;
    ``ephemera`` — снятое эфемерное и снимок вида ходов куска (§5.4)."""
    chunk = _Chunk(host, model, before, options, ephemera)
    for i in turns:
        chunk.turn(i)
    if nested is not None:
        chunk.nest(nested)
    return chunk.finish()


def history_level(turn: int) -> str:
    """Уровень блоков хода ``turn`` куска."""
    return f"{HISTORY_LEVEL_PREFIX}{turn}"


class _Chunk:
    """Один проход отрисовки куска: состояние только своё."""

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
        self._turn_of: dict[Widget, int] = {}
        self._view: ToolBlocksView | None = None
        self._turn: int | None = None
        self._flow = AnswerFlow()
        self._attempts: list[list[Widget]] = []
        # Вызовы без исхода: строка и вид её хода.
        self._open: dict[str, tuple[ToolCallRow, ToolBlocksView]] = {}
        self._finish_ids: set[str] = set()
        self._ended = False
        self._fresh_at = _fresh_outcome_index(model)

    # ---- монтаж ----------------------------------------------------------

    def mount(self, widget, *, before=None, after=None) -> None:
        """Хост блоков и событий куска: без якоря — перед ``before``
        куска; событие не из блоков закрывает открытую группу."""
        if before is None and after is None:
            before = self._before
        self._host.mount(widget, before=before, after=after)
        self._mounted.append(widget)
        if self._turn is not None:
            self._turn_of[widget] = self._turn
        view = self._view
        if view is not None and not view.issued(widget):
            view.event_closing()

    def _mount_all(self, widgets: Iterable[Widget]) -> None:
        for widget in widgets:
            self.mount(widget)

    def _turn_view(self) -> ToolBlocksView:
        """Вид блоков хода: один на ход, создаётся первым вызовом хода."""
        level = history_level(self._turn or 0)
        view = self.refs.block_views.get(level)
        if view is None:
            opts = self._opts
            model = ToolBlocks(opts.timers, diff_preview=opts.diff_preview)
            view = ToolBlocksView(self, model, level=level, depth=0)
            self.refs.block_views[level] = view
            self._view = view
        return view

    # ---- проход ----------------------------------------------------------

    def turn(self, i: int) -> None:
        self._turn = i
        self._flow = AnswerFlow()
        self._attempts = []
        self._ended = i + 1 < self._model.turn_count()
        start = self._model.turn_starts[i]
        rows = (
            self._ephemera.for_turn(self._model, i) if self._ephemera else []
        )
        for k, item in enumerate(self._model.turn_items(i), start):
            self._item(k, item)
            rows = self._mount_ephemeral(rows, k + 1)
        self._mount_ephemeral(rows, None)
        if self._ended:
            self._interrupt_open()

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
        calls = self.refs.call_rows
        self._mounted += nested.mount(
            self._host,
            nested.actions_for(set(calls)),
            calls,
            views=self.refs.block_views,
            options=self._opts,
        )

    def finish(self) -> ChunkRefs:
        if self._ephemera is not None:
            self._restore_view(self._ephemera)
        below = self._before
        if isinstance(below, ModelMark) and not is_being_removed(below):
            drop_mark_before(below)
        refs = self.refs
        refs.widgets = [w for w in self._mounted if not is_being_removed(w)]
        refs.turn_heads = self._turn_heads()
        mark = refs.last_model_mark
        if mark is not None and is_being_removed(mark):
            refs.last_model_mark = None
        return refs

    def _restore_view(self, ephemera: Ephemera) -> None:
        """Пометки снятых владельцев и снимок вида — виджетам куска."""
        for widget in self._mounted:
            for body in ephemera.restore_decorations(
                anchor_key(widget), widget
            ):
                widget.add_consequence(body)
            view_key = getattr(widget, "view_key", None)
            flag = ephemera.view(view_key()) if view_key else None
            if flag is not None:
                widget.restore_view(flag)
        for view in self.refs.block_views.values():
            view.restore_view(ephemera.view)

    def _turn_heads(self) -> dict[int, Widget]:
        """Первый в ленте виджет каждого хода: заголовок блока встаёт
        перед строкой своего хода, поэтому порядок — по ленте, а не по
        монтажу."""
        heads: dict[int, Widget] = {}
        turn_of = self._turn_of
        for widget in self._host.feed.children:
            turn = turn_of.get(widget)
            if turn is None or turn in heads or is_being_removed(widget):
                continue
            heads[turn] = widget
        return heads

    # ---- записи сбоку ----------------------------------------------------

    def _model_mark(self, rec: dict) -> None:
        record = parse_model_choice(rec)
        if record is None:
            return
        mark = restored_mark(record, self._opts.model_slots)
        self.mount(mark)
        drop_mark_before(mark)
        self.refs.last_model_mark = mark

    def _outcome(self, index: int, rec: dict) -> None:
        """Строка итога хода: свежая — только итог последнего хода, пока
        не началась реплика; время вызовов хода — застывшим таймерам."""
        outcome = parse_turn_outcome(rec)
        if outcome is None or outcome.opened_by in self._model.withdrawn:
            return
        end_turn(
            self.refs.block_views, self._opts.timers, outcome, settled=True
        )
        self._ended = True
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
        from gc_tui.widgets.user_reply import UserReplyContainer

        self._flow.new_turn()
        self._attempts = []
        self.mount(
            UserReplyContainer(
                ev.summary,
                event_no=ev.no,
                images=ev.attachments,
                skill=ev.skill or None,
            )
        )

    def _agent(self, ev: TraceEvent) -> None:
        if ev.tool_name == "submit_solution":
            self._finish(ev)
            return
        verdict = self._flow.agent(
            (ev.summary or "").strip(),
            intermediate=ev.tool_name == "intermediate_reply",
        )
        if not verdict.show:
            return
        if verdict.drop_attempt:
            self._drop_attempts()
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
        """Конец хода turn_finish: отчёт без повтора прозы; новая попытка
        убирает прошлые (отклонённые)."""
        close = self._flow.finish(
            finish_close_of(ev), tracked=bool(ev.call_id)
        )
        widgets = build_finish_widgets(close, event_no=ev.no)
        self._mount_all(widgets)
        if not ev.call_id:
            return
        self._finish_ids.add(ev.call_id)
        self._drop_attempts()
        self._attempts.append(widgets)

    def _drop_attempts(self) -> None:
        for widgets in self._attempts:
            for widget in widgets:
                widget.remove()
        self._attempts = []

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
        view = self._turn_view()
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
        if not call_id:
            return False
        return any(
            item.event is not None
            and item.event.kind == "ask_human"
            and item.event.call_id == call_id
            for item in self._model.items
        )

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
