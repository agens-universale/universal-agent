"""Построчный JSON-RPC 2.0: разбор stdin и запись stdout.

Транспорт не решает, какой метод ACP валиден. Он только отделяет request
от нотификации и не даёт двум записям смешать строки.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO, TextIO

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SESSION_NOT_FOUND = -32002

_ID_STRING = re.compile(r'"id"\s*:\s*"((?:\\.|[^"\\])*)"')
_ID_NUMBER = re.compile(r'"id"\s*:\s*(-?\d+)')

# Дефолтный потолок построчного чтения — 64 КиБ; длинный JSON-RPC (промпт IDE)
# должен доходить целиком. 16 МиБ хватает типичному session/prompt.
_STDIN_LINE_LIMIT = 16 * 1024 * 1024
# Префикс слишком длинной строки — только чтобы _salvage_id ответил -32700.
_OVERSIZE_SALVAGE = 256 * 1024
# Сколько байт за раз забирает блокирующий читатель. Меньше лимита строки:
# перевод строки ищется в накопленном буфере, а не в одном read.
_STDIN_READ_CHUNK = 64 * 1024


@dataclass(frozen=True)
class Incoming:
    """Одна непустая строка протокола."""

    id: Any = None
    has_id: bool = False
    method: str | None = None
    params: Any = None
    result: Any = None
    error: Any = None
    has_result: bool = False
    has_error: bool = False
    parse_error: bool = False
    not_object: bool = False


def parse_line(line: str) -> Incoming | None:
    """Разобрать строку. Пустая строка — None, битый JSON — parse_error."""
    if line.strip() == "":
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        salvaged = _salvage_id(line)
        return Incoming(
            id=salvaged,
            has_id=salvaged is not None,
            parse_error=True,
        )
    if not isinstance(payload, dict):
        return Incoming(not_object=True)
    method = payload.get("method")
    if not isinstance(method, str) or method == "":
        method = None
    raw_id = payload.get("id")
    has_id = (
        "id" in payload and raw_id is not None and not isinstance(raw_id, bool)
    )
    has_result = "result" in payload
    has_error = "error" in payload
    return Incoming(
        id=raw_id if has_id else None,
        has_id=has_id,
        method=method,
        params=payload.get("params"),
        result=payload.get("result") if has_result else None,
        error=payload.get("error") if has_error else None,
        has_result=has_result,
        has_error=has_error,
    )


def result_message(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_message(request_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def notification(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def _encode_line(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


class StdoutWriter:
    """Одна строка JSON и flush под общим lock.

    stream — поток протокола. В gc-acp это исходный stdout: после старта
    sys.stdout направлен в stderr, и print ядра не смешивается с JSON-RPC.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._lock = asyncio.Lock()
        self._stream = sys.stdout if stream is None else stream

    async def send(self, payload: dict) -> None:
        line = _encode_line(payload)
        async with self._lock:
            self._write(line)

    def write_locked(self, payload: dict) -> None:
        """Писать, уже держа ``_lock``. Повторный acquire из send — дедлок."""
        self._write(_encode_line(payload))

    def _write(self, line: str) -> None:
        self._stream.write(line)
        self._stream.flush()


def _discard_through_newline(stream: BinaryIO, pending: bytearray) -> bool:
    """Выбросить байты до ближайшего \\n включительно.

    False — EOF раньше перевода строки. Хвост после \\n остаётся в pending.
    """
    newline_at = pending.find(b"\n")
    if newline_at != -1:
        del pending[: newline_at + 1]
        return True
    pending.clear()
    while True:
        chunk = stream.read(_STDIN_READ_CHUNK)
        if not chunk:
            return False
        newline_at = chunk.find(b"\n")
        if newline_at == -1:
            continue
        pending.extend(chunk[newline_at + 1 :])
        return True


def _line_exceeds_limit(newline_at: int, pending_len: int) -> bool:
    """``newline_at < 0`` — перевода строки в буфере ещё нет."""
    if newline_at < 0:
        return pending_len > _STDIN_LINE_LIMIT
    return newline_at > _STDIN_LINE_LIMIT


def _pop_pending_line(pending: bytearray) -> tuple[bytes, bool] | None:
    """Снять одну строку. None — надо дочитать поток.

    True во втором элементе: отдан только префикс слишком длинной строки,
    хвост до ``\\n`` ещё лежит в буфере или во входном потоке.
    """
    newline_at = pending.find(b"\n")
    if _line_exceeds_limit(newline_at, len(pending)):
        return bytes(pending[:_OVERSIZE_SALVAGE]), True
    if newline_at < 0:
        return None
    end = newline_at + 1
    line = bytes(pending[:end])
    del pending[:end]
    return line, False


def _read_chunk(stream: BinaryIO, pending: bytearray) -> bool:
    """Дописать кусок в буфер. False — EOF, новых байт нет."""
    chunk = stream.read(_STDIN_READ_CHUNK)
    if not chunk:
        return False
    pending.extend(chunk)
    return True


def _blocking_stdin_lines(stream: BinaryIO) -> Iterator[bytes]:
    """Строки из бинарного stdin. Вызов блокируется и цикл не занимает.

    Обычная строка — байты до ``\\n`` включительно. Хвост без ``\\n`` на EOF
    тоже отдаётся. Длиннее ``_STDIN_LINE_LIMIT`` — только префикс
    ``_OVERSIZE_SALVAGE``, остаток до ``\\n`` к следующей строке не клеится.
    """
    pending = bytearray()
    while True:
        ready = _pop_pending_line(pending)
        if ready is None:
            if _read_chunk(stream, pending):
                continue
            if pending:
                yield bytes(pending)
            return
        line, discard_tail = ready
        yield line
        if discard_tail and not _discard_through_newline(stream, pending):
            return


async def stdin_lines() -> AsyncIterator[str]:
    """Строки stdin до EOF, не занимая цикл.

    Один поток процесса читает ``sys.stdin.buffer``. ``connect_read_pipe``
    на Windows регистрирует дескриптор в порте завершения: канал редактора
    для этого не открыт, и чтение падает с WinError 6. Политику цикла не
    меняем — Proactor нужен последующим subprocess.
    """
    loop = asyncio.get_running_loop()
    queued: asyncio.Queue[bytes | None] = asyncio.Queue()

    def post(item: bytes | None) -> None:
        try:
            loop.call_soon_threadsafe(queued.put_nowait, item)
        except RuntimeError:
            return

    def worker() -> None:
        try:
            stream = sys.stdin.buffer
            for raw in _blocking_stdin_lines(stream):
                post(raw)
        except OSError:
            print(
                "gc-acp: stdin read error",
                file=sys.stderr,
                flush=True,
            )
        finally:
            post(None)

    threading.Thread(target=worker, name="gc-acp-stdin", daemon=True).start()
    while True:
        raw = await queued.get()
        if raw is None:
            break
        line = _decode_line(raw)
        if line is None:
            print("gc-acp: parse error", file=sys.stderr)
            continue
        yield line


def _decode_line(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.endswith("\n"):
        text = text[:-1]
    if text.endswith("\r"):
        text = text[:-1]
    return text


class PendingOutbound:
    """Исходящие request агента: id → Future, пока клиент не ответит.

    Id — uuid, чтобы не пересечься с id запросов клиента. Ответ с чужим id
    сюда не попадает.
    """

    def __init__(self) -> None:
        self._items: dict[Any, tuple[str, asyncio.Future]] = {}

    def next_id(self) -> str:
        return str(uuid.uuid4())

    def add(
        self, request_id: Any, session_id: str, future: asyncio.Future
    ) -> None:
        self._items[request_id] = (session_id, future)

    def pop(self, request_id: Any) -> asyncio.Future | None:
        found = self._items.pop(request_id, None)
        if found is None:
            return None
        return found[1]

    def discard(self, request_id: Any) -> None:
        self._items.pop(request_id, None)

    def resolve(self, incoming: Incoming) -> bool:
        """Ответ на наш id. False — id не наш, dispatch идёт как раньше."""
        future = self.pop(incoming.id)
        if future is None:
            return False
        if not future.done():
            future.set_result(incoming)
        return True

    def cancel_id(self, request_id: Any, incoming: Incoming) -> bool:
        future = self.pop(request_id)
        if future is None:
            return False
        if not future.done():
            future.set_result(incoming)
        return True

    def cancel_session(self, session_id: str, incoming: Incoming) -> list[Any]:
        return self._settle(lambda _rid, sid: sid == session_id, incoming)

    def cancel_all(self, incoming: Incoming) -> list[Any]:
        return self._settle(lambda _rid, _sid: True, incoming)

    def _settle(self, predicate, incoming: Incoming) -> list[Any]:
        settled: list[Any] = []
        for request_id, (session_id, future) in list(self._items.items()):
            if not predicate(request_id, session_id):
                continue
            self._items.pop(request_id, None)
            settled.append(request_id)
            if not future.done():
                future.set_result(incoming)
        return settled


def _salvage_id(line: str) -> Any:
    """Достать id из битого JSON, чтобы ответить -32700 на request."""
    matched = _ID_STRING.search(line)
    if matched is not None:
        try:
            return json.loads('"' + matched.group(1) + '"')
        except json.JSONDecodeError:
            return None
    matched = _ID_NUMBER.search(line)
    if matched is None:
        return None
    return int(matched.group(1))
