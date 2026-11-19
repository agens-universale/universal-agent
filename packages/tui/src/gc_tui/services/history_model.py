"""Модель истории ленты: записи треда → события и записи сбоку в порядке
ленты (спека шага 2 окна ленты, §5.1).

Построение — чистая функция без виджетов: идёт в фоновом потоке и
тестируется напрямую. Порядок, номера и дедуп — те же, что у отрисовки
истории экраном (``TraceScreen._render_segments`` / ``_render_record``).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)
from gc_core.persistence.model_choice_record import (
    is_model_choice_record,
    parse_model_choice,
)
from gc_core.persistence.turn_outcome import (
    TurnOutcome,
    is_turn_outcome_record,
    parse_turn_outcome,
    turn_shown_in,
)

from gc_tui.models import TraceEvent
from gc_tui.services.answer_flow import AnswerFlow, replay_answer
from gc_tui.services.trace_mapper import (
    RenderContext,
    collect_render_context,
    map_message,
    response_tail,
)
from gc_tui.services.trace_render import build_compaction_marker_event
from gc_tui.services.trace_retention import (
    apply_payload_retention,
    events_leaving_window,
    message_ids_in_retention_window,
    retention_window_start_index,
)
from gc_tui.services.trace_steps import RESULT_KINDS, StepSplitter, shown

ItemKind = Literal["event", "outcome", "model_choice", "compaction"]


class HistoryBuildCancelled(Exception):
    """Построение остановлено: его результат больше не нужен."""


def _never() -> bool:
    return False


@dataclass(frozen=True)
class HistoryInputs:
    """Входы построения из настроек сессии: корень проекта (пути в подписях
    вызовов) и служебные поля схем тулов."""

    root: str | None
    injected_fields: tuple[str, ...]


@dataclass(frozen=True)
class HistoryItem:
    """Элемент ленты: событие (``event``, у маркера сжатия — тоже событие
    со своим номером) или запись сбоку (``side`` — итог хода, смена
    модели). Позиция — индекс в ``HistoryModel.items``."""

    kind: ItemKind
    event: TraceEvent | None = None
    side: dict | None = None


@dataclass
class HistoryModel:
    """История ленты открытого треда.

    ``turn_starts``, ``step_starts`` и ``segment_starts`` — индексы
    ``items``. Ход начинается настоящей репликой пользователя (ответ на
    вопрос агента и техническая запись хода не открывают); записи до
    первой реплики входят в первый ход (его начало — 0). Шаг — кусок хода
    по границам вызовов тулов (`trace_steps`), единица окна ленты; начало
    хода — всегда начало шага.

    После построения модель принадлежит экрану и меняется только на
    главном потоке: элементы дописываются через ``append_event`` /
    ``append_side`` (их зовут ``add_*``, общие для построения и живого
    пути), отзыв реплики — ``mark_withdrawn``, облегчение по окну
    удержания — ``apply_retention``."""

    context: RenderContext = field(default_factory=RenderContext)
    items: list[HistoryItem] = field(default_factory=list)
    turn_starts: list[int] = field(default_factory=list)
    step_starts: list[int] = field(default_factory=list)
    # Позиции (индекс следующего элемента ``items``) каждой настоящей
    # реплики в порядке файла, и повтора preserved-копии тоже: за ними
    # строка итога оседает (важна позиция в файле, а не повтор).
    reply_points: list[int] = field(default_factory=list)
    rendered_ids: set[str] = field(default_factory=set)
    response_tail: list = field(default_factory=list)
    segment_starts: list[int] = field(default_factory=list)
    next_no: int = 1
    last_model_choice: dict | None = None
    last_turn_outcome: TurnOutcome | None = None
    outcome_superseded: bool = False
    unanswered: set[str] = field(default_factory=set)
    withdrawn: set[str] = field(default_factory=set)
    window_ids: set[str] = field(default_factory=set)
    # Номер события → его индекс в ``items``.
    _item_of_no: dict[int, int] = field(default_factory=dict, repr=False)
    # call_id → событие вызова субагента (`subagent_*`), первое с ним.
    _subagent_calls: dict[str, TraceEvent] = field(
        default_factory=dict, repr=False
    )
    # call_id вопросов агента (`ask_human`).
    _question_ids: set[str] = field(default_factory=set, repr=False)
    # call_id → индексы событий вызова по порядку: бэкенд вправе повторить
    # id в позднем ходе. Исход принадлежит ближайшему вызову выше с тем
    # же id; первый показанный исход вызова и обратно — по индексам
    # (исход может лечь в следующий шаг, `trace_steps`).
    _calls: dict[str, list[int]] = field(default_factory=dict, repr=False)
    _result_of: dict[int, int] = field(default_factory=dict, repr=False)
    _call_of: dict[int, int] = field(default_factory=dict, repr=False)
    # Номера событий попыток сдачи хода, которые свернула следующая
    # попытка или проза, повторившая отчёт (R42): отрисовщик кусков их не
    # рисует, живой путь снимает, когда свёртка приходит. Правило — дедуп
    # ответа (`answer_flow.replay_answer`), как у отрисовки.
    collapsed_attempts: set[int] = field(default_factory=set)
    # Границы шагов дописываемого хода.
    _steps: StepSplitter = field(
        default_factory=StepSplitter, repr=False, compare=False
    )
    # Дедуп ответа по дописанным событиям — для свёртки попыток.
    _answers: AnswerFlow = field(
        default_factory=AnswerFlow, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        # Модель, собранная готовыми ходами, — шаг на ход.
        if self.turn_starts and not self.step_starts:
            self.step_starts = list(self.turn_starts)

    def event_by_no(self, no: int) -> TraceEvent | None:
        i = self._item_of_no.get(no)
        return None if i is None else self.items[i].event

    def events(self, since: int = 0) -> list[TraceEvent]:
        """События ленты (и маркеры сжатия) по порядку, с элемента
        ``since``."""
        return [it.event for it in self.items[since:] if it.event is not None]

    def subagent_calls(self) -> Iterable[TraceEvent]:
        """События вызовов субагентов по порядку ленты — без прохода по
        всем событиям."""
        return self._subagent_calls.values()

    def has_question(self, call_id: str | None) -> bool:
        """В ленте есть вопрос агента с этим call_id."""
        return bool(call_id) and call_id in self._question_ids

    def turn_index_of(self, no: int) -> int:
        """Ход события с номером ``no``; номера нет — ``KeyError``."""
        return bisect_right(self.turn_starts, self._item_of_no[no]) - 1

    def turn_items(self, i: int) -> list[HistoryItem]:
        start = self.turn_starts[i]
        if i + 1 < len(self.turn_starts):
            return self.items[start : self.turn_starts[i + 1]]
        return self.items[start:]

    def turn_count(self) -> int:
        return len(self.turn_starts)

    # ---- шаги (единица окна ленты) ---------------------------------------

    def step_count(self) -> int:
        return len(self.step_starts)

    def step_range(self, s: int) -> range:
        """Индексы ``items`` шага ``s``."""
        starts = self.step_starts
        stop = starts[s + 1] if s + 1 < len(starts) else len(self.items)
        return range(starts[s], stop)

    def step_turn(self, s: int) -> int:
        """Ход шага ``s``."""
        return bisect_right(self.turn_starts, self.step_starts[s]) - 1

    def turn_step(self, turn: int) -> int:
        """Первый шаг хода ``turn``; ``turn`` за последним — число шагов."""
        if turn >= len(self.turn_starts):
            return len(self.step_starts)
        return bisect_left(self.step_starts, self.turn_starts[turn])

    def starts_step(self, no: int) -> bool:
        """Событие с номером ``no`` — первый элемент своего шага: живой
        путь закрывает на нём группу вызовов (`trace_steps`)."""
        i = self._item_of_no.get(no)
        if i is None:
            return False
        at = bisect_left(self.step_starts, i)
        return at < len(self.step_starts) and self.step_starts[at] == i

    def item_step(self, i: int) -> int:
        """Шаг элемента ``i``."""
        return bisect_right(self.step_starts, i) - 1

    def call_before(self, call_id: str | None, index: int) -> int | None:
        """Индекс ближайшего события вызова ``call_id`` выше элемента
        ``index``."""
        calls = self._calls.get(call_id) if call_id else None
        if not calls:
            return None
        at = bisect_left(calls, index)
        return calls[at - 1] if at else None

    def result_of(self, call: int) -> int | None:
        """Индекс первого показанного исхода вызова на элементе ``call``
        (исход, ошибка, техническая ошибка с его call_id)."""
        return self._result_of.get(call)

    def call_of(self, result: int) -> int | None:
        """Индекс вызова, чей первый показанный исход — элемент
        ``result``; не первый исход — None."""
        return self._call_of.get(result)

    def last_reply(self, *, with_images: bool = False) -> TraceEvent | None:
        """Последняя реплика пользователя в ленте (снятые откатом не в
        счёт); ``with_images`` — последняя с картинками."""
        for item in reversed(self.items):
            ev = item.event
            if ev is None or ev.kind != "user" or ev.hidden:
                continue
            if ev.message_id is not None and ev.message_id in self.withdrawn:
                continue
            if not with_images or ev.attachments:
                return ev
        return None

    def last_outcome_index(self) -> int | None:
        """Индекс записи итога последнего хода: последняя запись итога,
        чья реплика не снята откатом; таких нет — None."""
        for i in range(len(self.items) - 1, -1, -1):
            item = self.items[i]
            if item.kind != "outcome":
                continue
            outcome = parse_turn_outcome(item.side)
            if outcome is not None and outcome.opened_by not in self.withdrawn:
                return i
        return None

    def open_turn(self) -> int | None:
        """Последний ход, если он не закончен: после его последнего
        события нет записи итога (ход идёт или оборван). Черта смены
        модели и маркер сжатия встают после итога и ход не продолжают.
        Закончен или ходов нет — None."""
        if not self.turn_starts:
            return None
        last = len(self.turn_starts) - 1
        for item in reversed(self.turn_items(last)):
            if item.kind == "outcome":
                return None
            if item.kind not in ("model_choice", "compaction"):
                return last
        return last

    def retention_start(self) -> int:
        """Первый элемент окна удержания payload: элементы до него — в
        частях треда, ушедших за окно (§3.5)."""
        i = retention_window_start_index(len(self.segment_starts))
        return self.segment_starts[i] if i > 0 else 0

    def is_withdrawn(self, payload: dict) -> bool:
        mid = payload.get("id")
        return mid is not None and mid in self.withdrawn

    # ---- изменение (главный поток) ---------------------------------------

    def append_event(self, ev: TraceEvent, kind: ItemKind = "event") -> None:
        """Событие с окончательным номером — в конец ленты."""
        self._item_of_no[ev.no] = len(self.items)
        self._note_call(ev)
        self._append(HistoryItem(kind, event=ev))
        if kind == "event" and shown(ev, self.withdrawn):
            replay_answer(self._answers, ev)
            self.collapsed_attempts.update(self._answers.collapsed)
        self.next_no = ev.no + 1
        if ev.tool_name == "ask_human" and ev.kind == "system" and ev.call_id:
            self.unanswered.add(ev.call_id)
        if not ev.call_id:
            return
        if ev.kind == "ask_human":
            self._question_ids.add(ev.call_id)
        elif ev.kind == "call" and str(ev.tool_name or "").startswith(
            "subagent_"
        ):
            self._subagent_calls.setdefault(ev.call_id, ev)

    def append_side(self, rec: dict, kind: ItemKind) -> None:
        """Запись сбоку (итог хода, смена модели) — в конец ленты."""
        self._append(HistoryItem(kind, side=rec))
        if kind == "outcome":
            self.last_turn_outcome = parse_turn_outcome(rec)
            self.outcome_superseded = False
        elif kind == "model_choice":
            self.last_model_choice = rec

    def _note_call(self, ev: TraceEvent) -> None:
        """Вызов и первый показанный исход после него: исход — ближайшему
        вызову выше с тем же call_id."""
        cid = ev.call_id
        if not cid or ev.hidden:
            return
        at = len(self.items)
        if ev.kind == "call":
            self._calls.setdefault(cid, []).append(at)
        elif ev.kind in RESULT_KINDS and cid in self._calls:
            call = self._calls[cid][-1]
            if call not in self._result_of:
                self._result_of[call] = at
                self._call_of[at] = call

    def _append(self, item: HistoryItem) -> None:
        """Элемент — в конец ленты и в шаги: начало хода — новый шаг, внутри
        хода границу решает `StepSplitter`. Пока ходов нет, нет и шагов;
        первый шаг, как и первый ход, начинается с 0."""
        at = len(self.items)
        self.items.append(item)
        if not self.turn_starts:
            return
        if not self.step_starts:
            self.step_starts.append(0)
        if self.turn_starts[-1] == at:
            self._steps.turn_started()
            if self.step_starts[-1] != at:
                self.step_starts.append(at)
            self._steps.feed(item, self.withdrawn)
        elif self._steps.feed(item, self.withdrawn):
            self.step_starts.append(at)

    def carry_response_tail(self, payloads: list, *, live: bool) -> list:
        """Хвост ответа после порции ``payloads``. Живой опрос несёт хвост
        прошлой пачки: текст ответа и его вызов приходят разными пачками.
        Возвращает payload порции вместе с хвостом."""
        if live:
            payloads = [*self.response_tail, *payloads]
        self.response_tail = response_tail(payloads)
        return payloads

    def supersede_outcome(self) -> None:
        """Начался новый ход: запись итога прошлого — уже не его строка."""
        self.outcome_superseded = True

    def restore_outcome(self) -> None:
        """Ход снят откатом: прошлый ход снова последний."""
        self.outcome_superseded = False

    def start_segment(self) -> None:
        """Следующие элементы — новая часть треда (после сжатия)."""
        self.segment_starts.append(len(self.items))

    def add_model_choice(self, rec: dict) -> bool:
        """Смена модели из записи треда; нечитаемая не входит."""
        if parse_model_choice(rec) is None:
            return False
        self.append_side(rec, "model_choice")
        return True

    def add_outcome(self, rec: dict) -> TurnOutcome | None:
        """Итог хода — строка последнего хода, пока не началась реплика.
        Сирота (сообщения её хода в истории нет) не входит."""
        if parse_turn_outcome(rec) is None:
            return None
        if not turn_shown_in(rec, self.rendered_ids):
            return None
        self.append_side(rec, "outcome")
        return self.last_turn_outcome

    def add_message(
        self, rec: dict, *, context: RenderContext | None, in_window: bool
    ) -> TraceEvent | None:
        """Сообщение треда → событие ленты; None — в ленту не входит
        (отозвано, уже показано, маппер его не рисует)."""
        payload = rec.get("payload")
        if not isinstance(payload, dict) or self.is_withdrawn(payload):
            return None
        # Реплика после итога вытесняет его — и повтором (важна позиция).
        real_reply = bool(extract_real_user_message_text(payload))
        if real_reply:
            self.outcome_superseded = True
            self.reply_points.append(len(self.items))
        mid = payload.get("id")
        if mid is not None:
            if mid in self.rendered_ids:
                return None
            self.rendered_ids.add(mid)
        ev = map_message(
            payload, no=self.next_no, ts=rec.get("ts", ""), context=context
        )
        if ev is None:
            return None
        apply_payload_retention(ev, seg_in_window=in_window)
        if not self.turn_starts:
            self.turn_starts.append(0)
        elif real_reply:
            self.turn_starts.append(len(self.items))
        self.append_event(ev)
        return ev

    def add_compaction_marker(
        self, segment_index: int, comp: dict
    ) -> TraceEvent:
        """Маркер сжатия части ``segment_index`` — событие со своим
        номером."""
        marker = build_compaction_marker_event(
            self.next_no, segment_index, comp
        )
        self.append_event(marker, "compaction")
        return marker

    def mark_withdrawn(self, message_id: str) -> None:
        """Откат реплики: отметка постоянная — реплика не входит в модель
        ни пересборкой, ни хвостом, даже когда её записи в файле уже нет.
        Запись итога её хода становится сиротой: итог последнего хода —
        снова прошлого."""
        self.withdrawn.add(message_id)
        self.rendered_ids.discard(message_id)
        last = self.last_turn_outcome
        if last is not None and last.opened_by == message_id:
            i = self.last_outcome_index()
            self.last_turn_outcome = (
                None if i is None else parse_turn_outcome(self.items[i].side)
            )

    def apply_retention(self, window_ids: set[str] | frozenset) -> None:
        """Шаг сжатия сдвинул окно удержания: события частей вне окна
        (кроме id окна) облегчаются, как при построении."""
        self.window_ids = set(window_ids)
        events = [it.event for it in self.items]
        for ev in events_leaving_window(
            events, self.segment_starts, self.window_ids
        ):
            if ev is not None:
                apply_payload_retention(ev, seg_in_window=False)


def build_history(
    segments: list[dict],
    *,
    inputs: HistoryInputs,
    withdrawn: set[str] | frozenset = frozenset(),
    cancelled: Callable[[], bool] = _never,
) -> HistoryModel:
    """Сегменты треда (формат ``FileTaskStore.read_history``) → модель.

    Все сегменты сырьём, маркер сжатия — между сегментом и следующим;
    preserved-копия (тот же id в следующей части) входит один раз;
    payload вызовов вне окна удержания облегчается. ``withdrawn`` —
    реплики, снятые откатом (их запись может ещё лежать в файле).
    ``cancelled`` спрашивается перед каждой записью: True —
    ``HistoryBuildCancelled`` (фоновое построение отменили)."""
    records = [rec for seg in segments for rec in seg["messages"]]
    payloads = [rec.get("payload") for rec in records if isinstance(rec, dict)]
    model = HistoryModel(
        context=collect_render_context(
            payloads,
            root=inputs.root,
            injected_fields=frozenset(inputs.injected_fields),
        ),
        response_tail=response_tail(payloads),
        withdrawn=set(withdrawn),
        window_ids=message_ids_in_retention_window(segments),
    )
    window_start = retention_window_start_index(len(segments))
    for i, seg in enumerate(segments):
        model.start_segment()
        for rec in seg["messages"]:
            if cancelled():
                raise HistoryBuildCancelled
            _add_record(model, rec, seg_in_window=i >= window_start)
        comp = seg["compacted_by"]
        if comp is not None:
            model.add_compaction_marker(i, comp)
    return model


def _add_record(
    model: HistoryModel, rec: dict, *, seg_in_window: bool
) -> None:
    if is_model_choice_record(rec):
        model.add_model_choice(rec)
    elif is_turn_outcome_record(rec):
        model.add_outcome(rec)
    else:
        payload = rec.get("payload")
        mid = payload.get("id") if isinstance(payload, dict) else None
        in_window = seg_in_window or (
            mid is not None and mid in model.window_ids
        )
        model.add_message(rec, context=model.context, in_window=in_window)
