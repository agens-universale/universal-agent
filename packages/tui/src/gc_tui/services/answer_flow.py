"""Какие ответы агента лента показывает и каким текстом.

Чистое правило без виджетов: лента и `/copy` (`export.agent_answers`) идут
по событиям треда через один `AnswerFlow`, поэтому номер ответа в `/copy N`
совпадает с тем, что видит человек.

Правила: реплика пользователя начинает ход (база дедупа и попытка
`turn_finish` сбрасываются); ответ, равный предыдущему ответу хода
(проза или промежуточный), не показывается; отчёт, равный ему, не
печатается вторым блоком; отчёт отклонённой попытки `turn_finish` уходит,
когда его повторила проза или пришла следующая попытка. Какие попытки
ушли последним событием — ``AnswerFlow.collapsed``: одно правило свёртки
для живого пути, отрисовщика кусков и модели истории (она помечает
попытки, которые свернутся, — R42)."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Optional

if TYPE_CHECKING:
    from gc_core.tools.turn.turn_close import TurnClose

    from gc_tui.models import TraceEvent


class AgentVerdict(NamedTuple):
    show: bool  # ответ рисуется
    drop_attempt: bool  # отчёт прошлой попытки turn_finish уходит
    # финальный ответ повторил промежуточный: лента его не рисует, но
    # этот текст и есть ответ хода (для `/copy`)
    final_of_intermediate: bool = False


class AnswerFlow:
    def __init__(self) -> None:
        self.last: Optional[str] = None  # база дедупа: прошлый ответ хода
        self.attempt: Optional[str] = None  # отчёт попытки turn_finish
        self._last_intermediate = False
        # последний `finish` повторил промежуточный ответ (для `/copy`)
        self.report_of_intermediate = False
        # ключи попыток turn_finish хода, которые ещё могут свернуться
        self._attempts: list[int] = []
        # ключи попыток, свернувшихся последним событием
        self.collapsed: list[int] = []

    def new_turn(self) -> None:
        self.last = None
        self.attempt = None
        self._last_intermediate = False
        self._attempts = []
        self.collapsed = []

    def agent(self, text: str, *, intermediate: bool = False) -> AgentVerdict:
        """Проза, промежуточный ответ или план (`text` уже после strip)."""
        self.collapsed = []
        if text and text == self.last:
            promote = self._last_intermediate and not intermediate
            self._last_intermediate = self._last_intermediate and not promote
            return AgentVerdict(False, False, promote)
        self._last_intermediate = intermediate
        drop = text is not None and text == self.attempt
        if drop:
            self.attempt = None
            self.collapsed, self._attempts = self._attempts, []
        self.last = text
        return AgentVerdict(True, drop)

    def finish(
        self, close: TurnClose, *, tracked: bool, key: int = 0
    ) -> TurnClose:
        """Конец хода turn_finish: отчёт без повтора прозы. `tracked` —
        у вызова есть call_id, попытку можно свернуть позже (`key` — её
        ключ в ``collapsed``); она заменяет прошлые."""
        self.collapsed = []
        # Ленивый импорт: пакет тулов тянет всё ядро, сервис грузится без него.
        from gc_core.tools.turn.turn_close import without_repeated_body

        body = close.body.strip()
        shown = bool(body) and body == (self.last or "")
        # как и у ответа: отчёт, повторивший промежуточный, — итог хода
        self.report_of_intermediate = shown and self._last_intermediate
        if self.report_of_intermediate:
            self._last_intermediate = False
        close = without_repeated_body(close, self.last or "")
        if tracked:
            self.attempt = close.body.strip() or None
            self.collapsed, self._attempts = self._attempts, [key]
        return close


def answer_verdict(flow: AnswerFlow, ev: TraceEvent) -> AgentVerdict:
    """Проза агента ``ev`` — в дедуп ответа хода: показывать ли её и
    уходит ли прошлая попытка сдачи. Одно правило отрисовщика кусков,
    живого пути и модели истории."""
    return flow.agent(
        (ev.summary or "").strip(),
        intermediate=ev.tool_name == "intermediate_reply",
    )


def finish_close(flow: AnswerFlow, ev: TraceEvent) -> TurnClose:
    """Сдача хода ``ev`` — в дедуп ответа хода: отчёт без повтора прозы;
    попытка с call_id (ключ — номер события) заменяет прошлые. Одно
    правило отрисовщика кусков, живого пути и модели истории."""
    # Ленивый импорт: модуль лёгкий, отчёт сдачи тянет ядро.
    from gc_tui.services.trace_render import finish_close_of

    return flow.finish(
        finish_close_of(ev), tracked=bool(ev.call_id), key=ev.no
    )


def replay_answer(flow: AnswerFlow, ev: TraceEvent) -> None:
    """Событие ``ev`` — в дедуп ответа хода так же, как его подаёт
    отрисовка: реплика начинает ход, сдача и проза — по своим правилам.
    Кусок, начатый внутри хода, проигрывает так элементы хода до себя;
    модель истории так узнаёт, какие попытки сдачи свернутся."""
    from gc_tui.services.trace_mapper import is_finish

    if ev.kind == "user":
        flow.new_turn()
    elif is_finish(ev):
        finish_close(flow, ev)
    elif ev.kind == "agent":
        answer_verdict(flow, ev)
