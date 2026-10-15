"""Пара текстов вокруг ``write_bytes`` → блок diff карточки тула.

Журнал ``agent_edits`` для этого не включается и второй раз не ставится:
обёртка только запоминает тексты и делегирует запись внутрь.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from gc_core.utils.file_text import decode_text_bytes

# Потолок одной стороны diff. Выше — в логе только размер, в карточке короткий текст.
_LIMIT = 256 * 1024
_OWN = ("_inner", "_log", "_cwd", "_call_id")


@dataclass
class _Text:
    text: str | None
    size: int
    known: bool


@dataclass
class _Side:
    old_text: str | None
    new_text: str | None
    old_size: int
    new_size: int
    known: bool


class DiffLog:
    """Пары одной сессии, свёрнутые по карточке и пути.

    Несколько ``write_bytes`` одного вызова в один файл — один diff:
    старый текст с первого чтения, новый с последней записи.
    Сторона тяжелее потолка в лог не кладётся: остаётся размер.
    ``content`` забирает вызов и больше его не держит.
    """

    def __init__(self) -> None:
        self._by_call: dict[str, dict[str, _Side]] = {}

    def add(self, call_id: str, path: str, old: _Text, new: _Text) -> None:
        known = old.known and new.known
        keep = known and old.size <= _LIMIT and new.size <= _LIMIT
        side = _Side(
            old.text if keep else None,
            new.text if keep else None,
            old.size,
            new.size,
            known,
        )
        bucket = self._by_call.setdefault(call_id, {})
        previous = bucket.get(path)
        if previous is None:
            bucket[path] = side
            return
        previous.new_text = side.new_text
        previous.new_size = side.new_size
        if previous.known and side.known:
            if previous.old_size > _LIMIT or previous.new_size > _LIMIT:
                previous.old_text = None
                previous.new_text = None
            return
        previous.known = False
        previous.old_text = None
        previous.new_text = None

    def content(self, call_id: str) -> list[dict] | None:
        bucket = self._by_call.pop(call_id, None)
        if not bucket:
            return None
        blocks: list[dict] = []
        for path, side in bucket.items():
            block = _content_block(path, side)
            if block is not None:
                blocks.append(block)
        if not blocks:
            return None
        return blocks


class DiffBackend:
    """Сессионная обёртка: до записи во внутренний бэкенд помнит тексты.

    Чтение, список и поиск идут во внутренний бэкенд, на диск.
    """

    def __init__(
        self,
        inner: Any,
        log: DiffLog,
        *,
        cwd: str,
        call_id: Callable[[], str | None],
    ) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_log", log)
        object.__setattr__(self, "_cwd", cwd)
        object.__setattr__(self, "_call_id", call_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _OWN:
            object.__setattr__(self, name, value)
            return
        setattr(self._inner, name, value)

    async def write_bytes(self, path: str, data: bytes) -> None:
        payload = data if isinstance(data, bytes) else bytes(data)
        old = await self._old_text(path)
        new = _text_of(payload)
        await self._inner.write_bytes(path, payload)
        call_id = self._call_id()
        if not isinstance(call_id, str) or call_id == "":
            return
        self._log.add(call_id, _absolute(path, self._cwd), old, new)

    async def _old_text(self, path: str) -> _Text:
        try:
            raw = await self._inner.read_bytes(path)
        except FileNotFoundError:
            return _Text(None, 0, True)
        except OSError:
            return _Text(None, 0, False)
        return _text_of(raw)


def attach_diff(update: dict, log: DiffLog | None) -> dict:
    """Дописать diff следом за превью. Превью остаётся первым блоком."""
    if log is None or update.get("sessionUpdate") != "tool_call_update":
        return update
    call_id = update.get("toolCallId")
    if not isinstance(call_id, str):
        return update
    extra = log.content(call_id)
    if not extra:
        return update
    shown = dict(update)
    preview = update.get("content")
    if isinstance(preview, list) and preview:
        shown["content"] = [*preview, *extra]
    else:
        shown["content"] = extra
    return shown


def _text_of(data: bytes) -> _Text:
    decoded = decode_text_bytes(data)
    text = decoded.text
    if not decoded.ok or not isinstance(text, str):
        return _Text(None, len(data), False)
    size = len(text.encode("utf-8"))
    if size > _LIMIT:
        return _Text(None, size, True)
    return _Text(text, size, True)


def _content_block(path: str, side: _Side) -> dict | None:
    if not side.known:
        return None
    if side.old_size > _LIMIT or side.new_size > _LIMIT:
        return {
            "type": "content",
            "content": {
                "type": "text",
                "text": _oversize_text(path, side.old_size, side.new_size),
            },
        }
    return {
        "type": "diff",
        "path": path,
        "oldText": side.old_text,
        "newText": side.new_text,
    }


def _oversize_text(path: str, old_size: int, new_size: int) -> str:
    return f"{path}: old {old_size} bytes, new {new_size} bytes"


def _absolute(path: str, cwd: str) -> str:
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(cwd, path))
