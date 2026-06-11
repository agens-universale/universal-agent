"""QueueStdin + redirect_stdin context manager — подмена sys.stdin для ask_human.

Агентский тул `ask_human` вызывает встроенный `input()`, который читает
`sys.stdin`. В TUI настоящий stdin занят Textual; чтобы TUI мог доставить ответ
пользователя в agent-поток, временно подменяем `sys.stdin` на очередь.
"""
from __future__ import annotations

import io
import queue
import sys
from contextlib import contextmanager
from typing import Iterator


class QueueStdin(io.TextIOBase):
    """File-like; .readline() блокирующе читает строку из queue."""

    def __init__(self, q: "queue.Queue[str]") -> None:
        super().__init__()
        self._q = q
        self._closed = False

    def readline(self, size: int = -1) -> str:  # type: ignore[override]
        if self._closed:
            return ""
        item = self._q.get()  # blocks; пустая строка = «закрытие потока»
        return item

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._closed = True
        super().close()


@contextmanager
def redirect_stdin(q: "queue.Queue[str]") -> Iterator[None]:
    """На время блока подменяет sys.stdin на QueueStdin(q).

    Textual читает клавиатуру через свой asyncio-драйвер (НЕ через sys.stdin),
    поэтому подмена sys.stdin его не задевает.
    """
    original = sys.stdin
    sys.stdin = QueueStdin(q)
    try:
        yield
    finally:
        sys.stdin = original
