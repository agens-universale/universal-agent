"""Построчный JSON-RPC 2.0: разбор stdin и запись stdout.

Транспорт не решает, какой метод ACP валиден. Он только отделяет request
от нотификации и не даёт двум записям смешать строки.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
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
    return Incoming(
        id=raw_id if has_id else None,
        has_id=has_id,
        method=method,
        params=payload.get("params"),
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


class StdoutWriter:
    """Одна строка JSON и flush под общим lock.

    stream — поток протокола. В gc-acp это исходный stdout: после старта
    sys.stdout направлен в stderr, и print ядра не смешивается с JSON-RPC.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._lock = asyncio.Lock()
        self._stream = sys.stdout if stream is None else stream

    async def send(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        async with self._lock:
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
