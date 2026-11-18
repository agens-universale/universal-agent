"""Шаги хода — единица окна ленты (спека шага 2 окна ленты, §5.3).

В реальной работе агента ход один на задачу и огромный: сотня вызовов
тулов — полторы тысячи виджетов. Окно, у которого минимальная единица —
ход, монтирует и снимает такой ход одним куском. Поэтому ход делится на
шаги по границам вызовов тулов, и окно домонтирует и снимает шаги.

Шаг начинается там, где отрисовщик кусков и без того начал бы с чистого
листа, — тогда ход, нарисованный по шагам, выглядит так же, как целиком:

- в начале хода (реплика пользователя) — всегда;
- внутри хода — на событии, которое смонтирует свой виджет не из блоков
  вызовов (проза агента, промежуточный ответ, план, чек-лист, вопрос,
  ответ на вопрос): оно закрывает группу вызовов, поэтому блок не
  разрежется между шагами;
- и только когда ход уже длиннее ``SPLIT_FROM_ITEMS`` элементов модели
  (ход короче и так не больше куска окна — он один шаг, как раньше), в
  шаге набралось не меньше ``STEP_MIN_ITEMS`` элементов, а сдачи
  ``submit_solution`` в ходе ещё не было (попытки сдачи сворачивают одна
  другую — они остаются в одном шаге).

Исход вызова может прийти в следующем шаге (параллельные вызовы, исход
после сжатия). Его виджеты встают якорем к строке вызова, а не в конец
ленты, поэтому отрисовщик применяет такой исход в конце шага вызова
(`HistoryModel.first_result`), а в шаге исхода пропускает его: строка
вызова и её исход всегда в одном шаге.

Проза, равная прошлому ответу хода, не рисуется (`AnswerFlow`) и группу не
закрывает: правило проверяется тем же `AnswerFlow`, что у отрисовщика.
Границы ставятся по мере дописывания модели и потом не меняются: номер
шага — устойчивый индекс окна и для живого хода. Модуль без Textual.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gc_tui.services.answer_flow import AnswerFlow

if TYPE_CHECKING:
    from gc_tui.models import TraceEvent
    from gc_tui.services.history_model import HistoryItem

#: Элементов модели в шаге не меньше стольких: раньше граница не ставится.
#: С оценкой ``ITEM_WIDGETS`` виджетов на элемент шаг не меньше куска
#: окна (``CHUNK_MIN``).
STEP_MIN_ITEMS = 10
#: Ход делится, только когда он длиннее стольких элементов: по оценке
#: ``ITEM_WIDGETS`` это ``CHUNK_MAX`` виджетов — ход короче и так встаёт
#: одним куском, и окно для него остаётся окном по ходам.
SPLIT_FROM_ITEMS = 24

#: Вызов сдачи хода: его попытки сворачивают одна другую.
_FINISH_TOOL = "submit_solution"
#: События, чей виджет не из блоков вызовов и монтируется всегда.
_CLOSING_KINDS = frozenset({"todo", "ask_human", "user"})
#: События исхода вызова: закрывают строку вызова с тем же call_id.
RESULT_KINDS = frozenset({"result", "error", "error_msg"})


def replay_answer(flow: AnswerFlow, ev: TraceEvent) -> bool:
    """Событие ``ev`` — в ``flow`` так же, как его подаёт отрисовщик
    (реплика сбрасывает ход, проза — дедуп). True — событие закрывает
    группу: его виджет будет смонтирован. Сдачу не подаёт (границ после
    неё нет)."""
    if ev.kind == "user":
        flow.new_turn()
        return True
    if ev.kind == "agent" and ev.tool_name != _FINISH_TOOL:
        verdict = flow.agent(
            (ev.summary or "").strip(),
            intermediate=ev.tool_name == "intermediate_reply",
        )
        return verdict.show
    return ev.kind in _CLOSING_KINDS


def shown(ev: TraceEvent | None, withdrawn: set[str]) -> bool:
    """Событие рисуется (не скрыто и не снято откатом)."""
    return (
        ev is not None
        and not ev.hidden
        and (ev.message_id is None or ev.message_id not in withdrawn)
    )


class StepSplitter:
    """Границы шагов по мере дописывания элементов модели."""

    _flow: AnswerFlow
    _finished: bool
    # Элементов в шаге и в ходе до дописываемого.
    _size: int
    _turn_size: int

    def __init__(self) -> None:
        self.turn_started()

    def turn_started(self) -> None:
        """Новый ход — новый шаг с чистым состоянием."""
        self._flow = AnswerFlow()
        self._finished = False
        self._size = 0
        self._turn_size = 0

    def feed(self, item: HistoryItem, withdrawn: set[str]) -> bool:
        """Элемент ``item`` дописан в ход; True — он начинает новый шаг."""
        ev = item.event
        starts = item.kind == "event" and shown(ev, withdrawn)
        if starts:
            assert ev is not None
            free = (
                not self._finished
                and self._size >= STEP_MIN_ITEMS
                and self._turn_size >= SPLIT_FROM_ITEMS
            )
            starts = replay_answer(self._flow, ev) and free
            if ev.kind == "agent" and ev.tool_name == _FINISH_TOOL:
                self._finished = True
        self._turn_size += 1
        self._size = 1 if starts else self._size + 1
        return starts
