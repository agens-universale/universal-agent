"""Текущий вызов тула: имя и ``call_id`` того, что сейчас исполняется.

Нейтральный модуль исполнения и единственная такая переменная: привязку
ставит ``aapply_tool`` вокруг тела тула, читают слои ниже инструментов —
шелл-делта журнала правок (правки команды привязаны к её вызову) и снятие
хода ``user_stop`` (событие стопа несёт тул и ``call_id``). Контекстная
переменная доезжает и в ``asyncio.to_thread``, и в задачи, созданные внутри.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

_CALL: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "gc_tool_call", default=("", "")
)


@contextmanager
def bind_tool_call(tool: str, call_id: str) -> Iterator[None]:
    token = _CALL.set((str(tool or ""), str(call_id or "")))
    try:
        yield
    finally:
        _CALL.reset(token)


def current_tool_call() -> tuple[str, str]:
    return _CALL.get()
