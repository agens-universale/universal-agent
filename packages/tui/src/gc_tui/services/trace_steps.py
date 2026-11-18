"""Шаги хода — единица окна ленты (спека шага 2 окна ленты, §5.3).

В реальной работе агента ход один на задачу и огромный: сотня вызовов
тулов — полторы тысячи виджетов. Окно, у которого минимальная единица —
ход, монтирует и снимает такой ход одним куском. Поэтому ход делится на
шаги, и окно домонтирует и снимает шаги.

Шаг рисуется своим видом блоков: группа вызовов границу шага не
пересекает — граница закрывает её на обоих путях (отрисовщик кусков
рисует шаг своим видом, живой путь закрывает группу на первом элементе
шага, `HistoryModel.starts_step`). Поэтому ход по шагам выглядит так же,
как целиком, а правила группировки и дедупа ответа отрисовщика
сплиттеру знать не нужно: где граница — решает только он.

Граница стоит на событии, которое встаёт своей строкой в конец ленты
(исход вызова — нет: его виджеты встают якорем к строке вызова):

- в начале хода (реплика пользователя) — всегда;
- мягкая — на событии не из вызовов (проза агента, промежуточный ответ,
  план, чек-лист, вопрос, ответ на вопрос): оно и так закрывает группу,
  вид ленты не меняется. Только когда ход уже длиннее ``SPLIT_FROM_ITEMS``
  элементов (ход короче и так не больше куска окна — он один шаг), а в
  шаге набралось ``STEP_MIN_ITEMS``;
- обязательная — на любом событии, пока оно и исход за ним (у вызова)
  помещаются в ``STEP_MAX_ITEMS`` элементов шага, а ни один вызов шага не
  ждёт исхода: строка вызова и её исход — в одном шаге. Серия вызовов без
  прозы так делится на шаги не больше куска окна; длинная серия
  однородных вызовов показывается несколькими группами — одинаково при
  открытии и вживую.

После сдачи хода (``turn_finish``) границ нет: попытки сдачи сворачивают
одна другую, отрисовщик держит их в одном шаге. Непокрыто (§5.3): остаток
хода после первой сдачи и вызов, оставшийся без исхода посреди хода
(обязательная граница ждёт его исхода), — такой остаток одним шагом.

Исход вызова, пришедший после мягкой границы (параллельные вызовы, исход
после сжатия), отрисовщик применяет в конце шага вызова
(`HistoryModel.first_result`). Границы ставятся по мере дописывания
модели и потом не меняются: номер шага — устойчивый индекс окна и для
живого хода. Модуль без Textual.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gc_tui.services import trace_window_policy as policy
from gc_tui.services.trace_mapper import FINISH_ID

if TYPE_CHECKING:
    from gc_tui.models import TraceEvent
    from gc_tui.services.history_model import HistoryItem

#: Элементов модели в шаге не больше стольких (до ближайшей
#: обязательной границы): по оценке ``ITEM_WIDGETS`` шаг не больше куска
#: окна ``CHUNK_MAX``.
STEP_MAX_ITEMS = policy.CHUNK_MAX // policy.ITEM_WIDGETS
#: Мягкая граница — не раньше стольких элементов в шаге: с оценкой
#: ``ITEM_WIDGETS`` шаг не меньше куска окна (``CHUNK_MIN``).
STEP_MIN_ITEMS = 10
#: Ход делится мягкими границами, только когда он длиннее стольких
#: элементов: ход не длиннее шага — один шаг, окно для него остаётся
#: окном по ходам.
SPLIT_FROM_ITEMS = STEP_MAX_ITEMS

#: События не из вызовов, кроме прозы агента: их строка закрывает группу.
_CLOSING_KINDS = frozenset({"todo", "ask_human", "user"})
#: События исхода вызова: закрывают строку вызова с тем же call_id.
RESULT_KINDS = frozenset({"result", "error", "error_msg"})


def shown(ev: TraceEvent | None, withdrawn: set[str]) -> bool:
    """Событие рисуется (не скрыто и не снято откатом)."""
    return (
        ev is not None
        and not ev.hidden
        and (ev.message_id is None or ev.message_id not in withdrawn)
    )


def is_finish(ev: TraceEvent) -> bool:
    """Событие — сдача хода (``turn_finish``)."""
    return ev.kind == "agent" and ev.tool_name == FINISH_ID


def _closes_group(ev: TraceEvent) -> bool:
    """Событие — строка не из вызовов (мягкая граница)."""
    if ev.kind == "agent":
        return not is_finish(ev)
    return ev.kind in _CLOSING_KINDS


class StepSplitter:
    """Границы шагов по мере дописывания элементов модели."""

    _finished: bool
    # Элементов в шаге и в ходе до дописываемого.
    _size: int
    _turn_size: int
    # Вызовы хода без исхода.
    _awaiting: set[str]

    def __init__(self) -> None:
        self.turn_started()

    def turn_started(self) -> None:
        """Новый ход — новый шаг с чистым состоянием."""
        self._finished = False
        self._size = 0
        self._turn_size = 0
        self._awaiting = set()

    def feed(self, item: HistoryItem, withdrawn: set[str]) -> bool:
        """Элемент ``item`` дописан в ход; True — он начинает новый шаг."""
        ev = item.event
        starts = False
        if item.kind == "event" and shown(ev, withdrawn):
            assert ev is not None
            starts = self._boundary(ev)
            self._note(ev)
        self._turn_size += 1
        self._size = 1 if starts else self._size + 1
        return starts

    def _boundary(self, ev: TraceEvent) -> bool:
        """Событие ``ev`` начинает шаг (правила — в докстроке модуля)."""
        if self._finished or ev.kind in RESULT_KINDS:
            return False
        if self._size + 1 >= STEP_MAX_ITEMS and not self._awaiting:
            return True
        return (
            _closes_group(ev)
            and self._size >= STEP_MIN_ITEMS
            and self._turn_size >= SPLIT_FROM_ITEMS
        )

    def _note(self, ev: TraceEvent) -> None:
        """Вызовы без исхода и сдача хода."""
        if ev.kind == "call" and ev.call_id:
            self._awaiting.add(ev.call_id)
        elif ev.kind in RESULT_KINDS and ev.call_id:
            self._awaiting.discard(ev.call_id)
        elif is_finish(ev):
            self._finished = True
