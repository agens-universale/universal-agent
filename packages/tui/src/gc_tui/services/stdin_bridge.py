"""Потокобезопасная подмена stdin для input() внутри agent-потоков (ask_human).

Агентский тул `ask_human` вызывает встроенный `input()`, который читает
`sys.stdin`. В TUI настоящий stdin занят Textual; чтобы TUI мог доставить ответ
пользователя в agent-поток, подменяем источник чтения на очередь.

Проблема старого подхода: redirect_stdin свапал ПРОЦЕССНО-ГЛОБАЛЬНЫЙ sys.stdin.
При наложении нескольких runner-потоков их `finally` затирали stdin друг друга →
input() заблокированного потока читал чужую очередь или реальный терминал (мусор
mouse/key-escapes) → ask_human виснет.

Решение: sys.stdin подменяется ОДИН раз на потоко-aware прокси; источник чтения
хранится в threading.local() — у каждого потока свой. Глобальный sys.stdin при
этом неизменен, гонки между потоками нет.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import queue

import io
import sys
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

_tls = threading.local()
_original_stdin: Optional[object] = None  # реальный stdin, захваченный при установке прокси


class QueueStdin(io.TextIOBase):
    """File-like; .readline() блокирующе читает строку из queue."""

    def __init__(self, q: queue.Queue[str]) -> None:
        super().__init__()
        self._q = q
        self._closed = False

    def readline(self, size: int = -1) -> str:  # type: ignore[override]
        if self._closed:
            return ""
        return self._q.get()  # blocks; пустая строка = «закрытие потока»

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._closed = True
        super().close()


class _ThreadAwareStdin(io.TextIOBase):
    """sys.stdin-прокси: readline() читает источник, назначенный ТЕКУЩЕМУ потоку
    (threading.local). Потоки без назначенного источника падают на оригинальный
    stdin. Глобальный sys.stdin один и неизменен — потоки не затирают друг друга."""

    def readline(self, size: int = -1) -> str:  # type: ignore[override]
        src = getattr(_tls, "stdin", None)
        if src is not None:
            return src.readline(size)
        if _original_stdin is not None:
            return _original_stdin.readline(size)
        return ""

    def readable(self) -> bool:
        return True


def _install_proxy() -> None:
    """Один раз подменить sys.stdin на потоко-aware прокси (идемпотентно)."""
    global _original_stdin
    if not isinstance(sys.stdin, _ThreadAwareStdin):
        _original_stdin = sys.stdin
        sys.stdin = _ThreadAwareStdin()


@contextmanager
def redirect_stdin(q: queue.Queue[str]) -> Iterator[None]:
    """На время блока назначает ТЕКУЩЕМУ потоку QueueStdin(q) как источник input().

    Не свапает глобальный sys.stdin (ставит один раз потоко-aware прокси) —
    параллельные runner-потоки не затирают stdin друг друга. Textual читает
    клавиатуру через свой asyncio-драйвер (НЕ через sys.stdin), прокси его не задевает.
    """
    _install_proxy()
    prev = getattr(_tls, "stdin", None)
    _tls.stdin = QueueStdin(q)
    try:
        yield
    finally:
        _tls.stdin = prev
