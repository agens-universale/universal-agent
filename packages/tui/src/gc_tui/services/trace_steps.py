"""Шаги хода — единица окна ленты (спека шага 2 окна ленты, §5.3).

В реальной работе агента ход один на задачу и огромный: сотня вызовов
тулов — полторы тысячи виджетов. Окно, у которого минимальная единица —
ход, монтирует и снимает такой ход одним куском. Поэтому ход делится на
шаги, и окно домонтирует и снимает шаги.

Шаг рисуется своим видом блоков: группа вызовов границу шага не
пересекает — граница закрывает её на обоих путях (отрисовщик кусков
рисует шаг своим видом, живой путь закрывает группу на первом элементе
шага, `HistoryModel.starts_step`). Поэтому ход по шагам выглядит так же,
как целиком; где граница — решает только сплиттер. Чтобы граница не
резала группу без нужды, он спрашивает правило группировки отрисовщика —
ту же функцию, `tool_blocks.may_join` (R50).

Граница стоит на событии, которое встаёт своей строкой в конец ленты
(исход вызова — нет: его виджеты встают якорем к строке вызова; попытка
сдачи — тоже нет: следующая попытка её сворачивает, и шаг не начинается
виджетами, которые снимаются мимо окна):

- в начале хода (реплика пользователя) — всегда;
- мягкая — на событии не из вызовов (проза агента, промежуточный ответ,
  план, чек-лист, вопрос, ответ на вопрос): оно и так закрывает группу,
  вид ленты не меняется. Только когда ход уже длиннее ``SPLIT_FROM_ITEMS``
  элементов (ход короче и так не больше куска окна — он один шаг), а в
  шаге набралось ``STEP_MIN_ITEMS``;
- обязательная — когда шаг дорос до ``STEP_MAX_ITEMS`` элементов, а ни
  один вызов шага не ждёт исхода (строка вызова и её исход — в одном
  шаге). Встаёт она в последнем месте шага (кроме его начала), где группа
  вызовов закрылась бы и так (вызов другого вида, нарисованное событие не
  из вызовов), — задним числом, если это место уже позади (R50, R51);
  такого места нет (одна длинная однородная серия) — на пределе, и серия
  показывается несколькими группами — одинаково при открытии и вживую.
  Позже предела граница не встаёт. Сплиттер консервативен: он видит не
  все закрытия отрисовщика (мысли, черта модели, строка «!»), только те,
  что закрывают группу наверняка. Пачку параллельных вызовов граница не
  режет (вызовы пачки ждут исходов), поэтому предел шага —
  ``STEP_MAX_ITEMS`` плюс одна пачка: до ``STEP_MAX_ITEMS - 2 + 2 ×``
  ширина пачки элементов (R43). Гигантского шага так нет, но
  ``CHUNK_MAX`` для шага с широкой пачкой — не жёсткая граница.

Границы стоят и после сдачи хода (``turn_finish``, R42): попытку, которую
свернёт следующая попытка или проза, повторившая её отчёт, модель
помечает заранее (`HistoryModel.collapsed_attempts`), и отрисовщик
кусков её не рисует, в каком бы шаге ни лежала свёртка. Непокрыто
(§5.3): вызов, оставшийся без исхода посреди хода (обязательная граница
ждёт его исхода), — остаток хода за ним одним шагом.

Исход вызова, пришедший после мягкой границы (параллельные вызовы, исход
после сжатия), отрисовщик применяет в конце шага вызова
(`HistoryModel.result_of`). Границы ставятся по мере дописывания
модели; граница задним числом встаёт только внутри последнего шага, и
номера прежних шагов не меняются. Уже нарисованные виджеты переходят в
новый шаг сами: метка виджета — элемент модели, шаг — по модели
(`trace_window`). Модуль без Textual.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gc_tui.services import trace_window_policy as policy
from gc_tui.services.tool_blocks import BlockKind, call_block_kind, may_join
from gc_tui.services.trace_mapper import is_finish

if TYPE_CHECKING:
    from gc_tui.models import TraceEvent
    from gc_tui.services.history_model import HistoryItem

#: Обязательная граница — как только шаг дорос до стольких элементов и
#: ни один его вызов не ждёт исхода: по оценке ``ITEM_WIDGETS`` такой шаг
#: не больше куска окна ``CHUNK_MAX``; шаг с пачкой параллельных вызовов
#: больше на пачку (R43).
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


def _closes_group(ev: TraceEvent) -> bool:
    """Событие — строка не из вызовов (мягкая граница)."""
    if ev.kind == "agent":
        return not is_finish(ev)
    return ev.kind in _CLOSING_KINDS


class StepSplitter:
    """Границы шагов по мере дописывания элементов модели."""

    # Элементов в шаге и в ходе до дописываемого.
    _size: int
    _turn_size: int
    # Вызовы хода без исхода.
    _awaiting: set[str]
    # Вид блока, открытого над следующим вызовом (`tool_blocks.may_join`);
    # None — над ним закрывающее событие или ничего.
    _open: BlockKind | None
    # Места в шаге (число элементов шага перед ними; не само начало шага),
    # где группа вызовов закрылась бы и так, а вызова без исхода не было:
    # туда обязательная граница встаёт задним числом (R50, R51 — любое
    # такое место, шаг из одного элемента дёшев).
    _closes: list[int]

    def __init__(self) -> None:
        self.turn_started()

    def turn_started(self) -> None:
        """Новый ход — новый шаг с чистым состоянием."""
        self._size = 0
        self._turn_size = 0
        self._awaiting = set()
        self._open = None
        self._closes = []

    def feed(
        self, item: HistoryItem, withdrawn: set[str], *, drawn: bool = True
    ) -> int | None:
        """Элемент ``item`` дописан в ход (``drawn`` — False у прозы, которую
        дедуп ответа не рисует). Новый шаг: None — нет; 0 — его начинает
        этот элемент; ``k > 0`` — шаг начинается за ``k`` элементов до
        него, в последнем месте шага, где группа вызовов и так
        закрылась."""
        ev = item.event
        back = None
        closes = False
        if item.kind == "event" and shown(ev, withdrawn):
            assert ev is not None
            closes = self._group_closes(ev, drawn)
            back = self._boundary(ev, closes)
        self._turn_size += 1
        if back is None:
            self._size += 1
        else:
            cut = self._size - back
            self._closes = [o - cut for o in self._closes if o > cut]
            self._size = back + 1
        if item.kind == "event" and shown(ev, withdrawn):
            self._note(ev, closes, drawn)
        return back

    def _group_closes(self, ev: TraceEvent, drawn: bool) -> bool:
        """Группа вызовов над ``ev`` закрывается перед ним — по правилу
        отрисовщика (`tool_blocks.may_join`): вызов другого вида,
        нарисованное событие не из вызовов. Исход и сдача хода (её может
        свернуть следующая) группу не трогают."""
        if ev.kind == "call":
            kind = call_block_kind(ev.wire_name or ev.tool_name or "")
            return kind is not None and not may_join(self._open, kind)
        if ev.kind in RESULT_KINDS or is_finish(ev):
            return False
        return drawn

    def _boundary(self, ev: TraceEvent, closes: bool) -> int | None:
        """Где начинается шаг (правила — в докстроке модуля): None — не
        здесь."""
        if ev.kind in RESULT_KINDS or is_finish(ev):
            return None
        if self._size + 1 >= STEP_MAX_ITEMS and not self._awaiting:
            if closes or not self._closes:
                return 0
            return self._size - self._closes[-1]
        if (
            _closes_group(ev)
            and self._size >= STEP_MIN_ITEMS
            and self._turn_size >= SPLIT_FROM_ITEMS
        ):
            return 0
        return None

    def _note(self, ev: TraceEvent, closes: bool, drawn: bool) -> None:
        """Вызовы без исхода, открытый вид блока и места, где группа
        закрылась бы сама."""
        position = self._size - 1
        if closes and position > 0 and not self._awaiting:
            self._closes.append(position)
        if ev.kind == "call":
            kind = call_block_kind(ev.wire_name or ev.tool_name or "")
            if kind is not None:
                self._open = kind
            if ev.call_id:
                self._awaiting.add(ev.call_id)
        elif ev.kind in RESULT_KINDS:
            if ev.call_id:
                self._awaiting.discard(ev.call_id)
        elif drawn and not is_finish(ev):
            self._open = None
