"""Какие ответы агента лента показывает и каким текстом.

Чистое правило без виджетов: лента и `/copy` (`export.agent_answers`) идут
по событиям треда через один `AnswerFlow`, поэтому номер ответа в `/copy N`
совпадает с тем, что видит человек.

Правила: реплика пользователя начинает ход (база дедупа и попытка
`turn_finish` сбрасываются); ответ, равный предыдущему ответу хода
(проза или промежуточный), не показывается; отчёт, равный ему, не
печатается вторым блоком; отчёт отклонённой попытки `turn_finish` уходит,
когда его повторила проза или пришла следующая попытка."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Optional

if TYPE_CHECKING:
    from gc_core.tools.turn.turn_close import TurnClose


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

    def new_turn(self) -> None:
        self.last = None
        self.attempt = None
        self._last_intermediate = False

    def agent(self, text: str, *, intermediate: bool = False) -> AgentVerdict:
        """Проза, промежуточный ответ или план (`text` уже после strip)."""
        if text and text == self.last:
            promote = self._last_intermediate and not intermediate
            self._last_intermediate = self._last_intermediate and not promote
            return AgentVerdict(False, False, promote)
        self._last_intermediate = intermediate
        drop = text is not None and text == self.attempt
        if drop:
            self.attempt = None
        self.last = text
        return AgentVerdict(True, drop)

    def finish(self, close: TurnClose, *, tracked: bool) -> TurnClose:
        """Конец хода turn_finish: отчёт без повтора прозы. `tracked` —
        у вызова есть call_id, попытку можно свернуть позже; она заменяет
        прошлую."""
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
        return close
