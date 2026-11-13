"""Модель истории ленты: записи треда → события и записи сбоку в порядке
ленты (спека шага 2 окна ленты, §5.1).

Построение — чистая функция без виджетов: идёт в фоновом потоке и
тестируется напрямую. Порядок, номера и дедуп — те же, что у отрисовки
истории экраном (``TraceScreen._render_segments`` / ``_render_record``).
"""

from __future__ import annotations

from bisect import bisect_right
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
from gc_tui.services.trace_mapper import (
    RenderContext,
    collect_render_context,
    map_message,
    response_tail,
)
from gc_tui.services.trace_render import build_compaction_marker_event
from gc_tui.services.trace_retention import (
    apply_payload_retention,
    message_ids_in_retention_window,
    retention_window_start_index,
)

ItemKind = Literal["event", "outcome", "model_choice", "compaction"]


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

    ``turn_starts`` и ``segment_starts`` — индексы ``items``. Ход
    начинается настоящей репликой пользователя (ответ на вопрос агента и
    техническая запись хода не открывают); записи до первой реплики
    входят в первый ход (его начало — 0)."""

    context: RenderContext
    items: list[HistoryItem] = field(default_factory=list)
    turn_starts: list[int] = field(default_factory=list)
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
    # Индекс в ``items`` события с номером ``no`` — ``_item_of_no[no - 1]``.
    _item_of_no: list[int] = field(default_factory=list, repr=False)

    def event_by_no(self, no: int) -> TraceEvent | None:
        if not 1 <= no <= len(self._item_of_no):
            return None
        return self.items[self._item_of_no[no - 1]].event

    def turn_index_of(self, no: int) -> int:
        """Ход события с номером ``no``; номера нет — ``KeyError``."""
        if self.event_by_no(no) is None:
            raise KeyError(no)
        return bisect_right(self.turn_starts, self._item_of_no[no - 1]) - 1

    def turn_items(self, i: int) -> list[HistoryItem]:
        start = self.turn_starts[i]
        if i + 1 < len(self.turn_starts):
            return self.items[start : self.turn_starts[i + 1]]
        return self.items[start:]

    def turn_count(self) -> int:
        return len(self.turn_starts)

    def _append_event(self, ev: TraceEvent, kind: ItemKind = "event") -> None:
        self._item_of_no.append(len(self.items))
        self.items.append(HistoryItem(kind, event=ev))
        self.next_no = ev.no + 1


def build_history(
    segments: list[dict], *, inputs: HistoryInputs
) -> HistoryModel:
    """Сегменты треда (формат ``FileTaskStore.read_history``) → модель.

    Все сегменты сырьём, маркер сжатия — между сегментом и следующим;
    preserved-копия (тот же id в следующей части) входит один раз;
    payload вызовов вне окна удержания облегчается."""
    records = [rec for seg in segments for rec in seg["messages"]]
    payloads = [rec.get("payload") for rec in records if isinstance(rec, dict)]
    model = HistoryModel(
        context=collect_render_context(
            payloads,
            root=inputs.root,
            injected_fields=frozenset(inputs.injected_fields),
        ),
        response_tail=response_tail(payloads),
        window_ids=message_ids_in_retention_window(segments),
    )
    window_start = retention_window_start_index(len(segments))
    for i, seg in enumerate(segments):
        model.segment_starts.append(len(model.items))
        for rec in seg["messages"]:
            _add_record(model, rec, seg_in_window=i >= window_start)
        comp = seg["compacted_by"]
        if comp is not None:
            marker = build_compaction_marker_event(model.next_no, i, comp)
            model._append_event(marker, "compaction")
    return model


def _add_record(
    model: HistoryModel, rec: dict, *, seg_in_window: bool
) -> None:
    if is_model_choice_record(rec):
        if parse_model_choice(rec) is not None:
            model.items.append(HistoryItem("model_choice", side=rec))
            model.last_model_choice = rec
        return
    if is_turn_outcome_record(rec):
        _add_outcome(model, rec)
        return
    payload = rec.get("payload")
    if isinstance(payload, dict):
        _add_message(model, rec, payload, seg_in_window=seg_in_window)


def _add_outcome(model: HistoryModel, rec: dict) -> None:
    """Итог хода — строка последнего хода, пока не началась реплика.
    Сирота (сообщения её хода в истории нет) не входит."""
    outcome = parse_turn_outcome(rec)
    if outcome is None or not turn_shown_in(rec, model.rendered_ids):
        return
    model.items.append(HistoryItem("outcome", side=rec))
    model.last_turn_outcome = outcome
    model.outcome_superseded = False


def _add_message(
    model: HistoryModel, rec: dict, payload: dict, *, seg_in_window: bool
) -> None:
    # Реплика после итога вытесняет его — и повтором (важна позиция).
    real_reply = bool(extract_real_user_message_text(payload))
    if real_reply:
        model.outcome_superseded = True
    mid = payload.get("id")
    if mid is not None:
        if mid in model.rendered_ids:
            return
        model.rendered_ids.add(mid)
    ev = map_message(
        payload, no=model.next_no, ts=rec.get("ts", ""), context=model.context
    )
    if ev is None:
        return
    in_window = seg_in_window or (mid is not None and mid in model.window_ids)
    apply_payload_retention(ev, seg_in_window=in_window)
    if not model.turn_starts:
        model.turn_starts.append(0)
    elif real_reply:
        model.turn_starts.append(len(model.items))
    model._append_event(ev)
    if ev.tool_name == "ask_human" and ev.kind == "system" and ev.call_id:
        model.unanswered.add(ev.call_id)
