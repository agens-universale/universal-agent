"""Привязка к одному вызову инструмента.

Ставит и снимает её только диспетчер (`gc_core.tools`). Защиты читают отсюда
то, что принадлежит именно этому вызову: настоящее состояние прогона (тулы с
`owns_session_state` получают от диспетчера лишь срез без переписки),
идентификатор вызова, подготовленные аргументы и разовые ответы человека.

Привязка — изменяемый объект в контекстной переменной: защита, исполняемая в
потоке с копией контекста, пишет в тот же объект, а два одновременных вызова
(родитель и субагент делят одно хранилище решений) друг друга не видят.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CallScope:
    state: Any
    call_id: str | None
    tool_name: str
    #: Аргументы после разбора диспетчером — то, что реально исполнится.
    arguments: Any = None
    #: Разовые ответы человека («разрешить / запретить разово»).
    one_time_answers: dict[str, bool] = field(default_factory=dict)
    #: Пометки, с которыми судья действий одобрил этот вызов; None — не одобрял.
    #: Следующая стадия того же вызова зовёт судью снова, только если несёт
    #: пометку, которой здесь нет.
    judge_approved_flags: frozenset[str] | None = None
    #: Пол команд нашёл в команде защищённый путь и отдал решение судье
    #: действий. Пол и стадия режима — два разных декоратора: пол ставит
    #: признак, стадия по нему зовёт судью даже для безобидной команды.
    floor_review: bool = False


_current: ContextVar[CallScope | None] = ContextVar(
    "gc_tool_call_scope", default=None
)


def current_call() -> CallScope | None:
    """Привязка текущего вызова; None — код исполняется вне диспетчера."""
    return _current.get()


@contextmanager
def call_scope(
    *, state: Any, call_id: str | None, tool_name: str
) -> Iterator[CallScope]:
    scope = CallScope(state=state, call_id=call_id, tool_name=tool_name)
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)
