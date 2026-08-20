"""Читатель stdio MCP: один поток на процесс (план async-переезда, Задача 20).

Прежняя реализация заводила поток на каждую строку JSON-RPC — до 32 на один
вызов тула — и при таймауте оставляла его висеть на readline навсегда.
"""
import io
import threading
import time

from gc_core.tools.connect_mcp.transport import _readline_with_timeout, forget_stdio_reader


class _SlowStream:
    """Поток, который отдаёт строки по требованию и умеет «замолчать»."""

    def __init__(self, lines, *, hang_after=None):
        self._lines = list(lines)
        self._hang_after = hang_after
        self._served = 0
        self.hanging = threading.Event()

    def readline(self):
        if self._hang_after is not None and self._served >= self._hang_after:
            self.hanging.set()
            time.sleep(30)          # имитируем молчащий сервер
            return ""
        if not self._lines:
            return ""
        self._served += 1
        return self._lines.pop(0)


def test_reads_lines_in_order():
    stream = _SlowStream(["первая\n", "вторая\n"])
    try:
        assert _readline_with_timeout(stream, 2).strip() == "первая"
        assert _readline_with_timeout(stream, 2).strip() == "вторая"
    finally:
        forget_stdio_reader(stream)


def test_one_thread_per_stream_not_per_line(monkeypatch):
    """Ради этого всё и делалось: 10 строк — один поток, а не десять.

    Считаем СОЗДАННЫЕ потоки, а не живые: читатель успевает завершиться, дочитав
    поток до конца, и счётчик живых потоков этого бы не показал.
    """
    created = []
    real_thread = threading.Thread

    def _counting(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        created.append(kwargs.get("name") or t.name)
        return t

    monkeypatch.setattr(threading, "Thread", _counting)
    stream = _SlowStream([f"строка {i}\n" for i in range(10)])
    try:
        for _ in range(10):
            _readline_with_timeout(stream, 2)
        readers = [n for n in created if n == "mcp-stdio-reader"]
        assert len(readers) == 1, f"потоков-читателей {len(readers)} на 10 строк"
    finally:
        forget_stdio_reader(stream)


def test_timeout_returns_none_without_leaking_a_thread_per_call():
    """Молчащий сервер: таймаут отдаёт None, но потоки не копятся."""
    stream = _SlowStream(["одна\n"], hang_after=1)
    try:
        assert _readline_with_timeout(stream, 2).strip() == "одна"
        before = threading.active_count()
        for _ in range(5):
            assert _readline_with_timeout(stream, 0.05) is None
        # Раньше каждый такой вызов оставлял по висящему потоку.
        assert threading.active_count() == before
    finally:
        forget_stdio_reader(stream)


def test_closed_stream_stops_returning_lines():
    """После конца потока читатель отвечает None сразу, а не по таймауту."""
    stream = _SlowStream(["всё\n"])
    try:
        assert _readline_with_timeout(stream, 2).strip() == "всё"
        started = time.monotonic()
        assert _readline_with_timeout(stream, 5) is None
        assert time.monotonic() - started < 1, "ждали таймаут вместо маркера конца"
    finally:
        forget_stdio_reader(stream)


def test_bytes_lines_are_decoded():
    stream = _SlowStream([b'{"jsonrpc":"2.0"}\n'])
    try:
        assert '"jsonrpc"' in _readline_with_timeout(stream, 2)
    finally:
        forget_stdio_reader(stream)
