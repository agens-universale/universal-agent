"""Построчный JSON-RPC 2.0: разбор stdin и запись stdout.

Транспорт не решает, какой метод ACP валиден. Он только отделяет request
от нотификации и не даёт двум записям смешать строки.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, TextIO

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SESSION_NOT_FOUND = -32002

_ID_STRING = re.compile(r'"id"\s*:\s*"((?:\\.|[^"\\])*)"')
_ID_NUMBER = re.compile(r'"id"\s*:\s*(-?\d+)')

# asyncio.StreamReader по умолчанию — 64 KiB; длинный JSON-RPC (промпт IDE) роняет
# readline() и весь процесс gc-acp. 16 MiB хватает для типичных session/prompt.
_STDIN_LINE_LIMIT = 16 * 1024 * 1024
# Префикс слишком длинной строки — только чтобы _salvage_id ответил -32700.
_OVERSIZE_SALVAGE = 256 * 1024


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


async def _read_stdin_line(reader: asyncio.StreamReader) -> bytes:
    """Одна строка протокола (до \\n). Не readline(): при превышении limit он
    выбрасывает строку из буфера без возврата вызывающему."""
    try:
        return await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        return exc.partial
    except asyncio.LimitOverrunError as exc:
        isep = exc.consumed
        if not isinstance(isep, int):
            raise
        line = await reader.read(isep + 1)
        if len(line) <= _OVERSIZE_SALVAGE:
            return line
        return line[:_OVERSIZE_SALVAGE]


async def stdin_lines() -> AsyncIterator[str]:
    """Читать stdin до EOF, не ожидая конца хода."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=_STDIN_LINE_LIMIT)
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    while True:
        raw = await _read_stdin_line(reader)
        if not raw:
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

    def cancel_session(self, session_id: str, incoming: Incoming) -> None:
        self._settle(lambda _rid, sid: sid == session_id, incoming)

    def cancel_all(self, incoming: Incoming) -> None:
        self._settle(lambda _rid, _sid: True, incoming)

    def _settle(self, predicate, incoming: Incoming) -> None:
        for request_id, (session_id, future) in list(self._items.items()):
            if not predicate(request_id, session_id):
                continue
            self._items.pop(request_id, None)
            if not future.done():
                future.set_result(incoming)


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
