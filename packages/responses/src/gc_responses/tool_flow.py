"""Хвост jsonl треда → короткая карточка тула.

Сырые ``arguments`` и тело ``output`` в карточку не попадают.
``turn_finish``, ``turn_ask_human``, ``turn_intermediate_reply`` и
``session_think`` наружу не выходят.
"""

from __future__ import annotations

import json
import os
from typing import Any

from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    FINISH_TOOL_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
    SESSION_THINK_TOOL_NAME,
)
from gc_core.tools.subagents.progress import (
    brief_args_summary,
    parse_tool_result_status,
)

_CALL = "function_call"
_CALL_OUTPUT = "function_call_output"
_ENVELOPE = "message"
_IN_PROGRESS = "in_progress"
_COMPLETED = "completed"
_FAILED = "failed"
_HIDDEN = frozenset(
    {
        FINISH_TOOL_NAME,
        ASK_HUMAN_TOOL_NAME,
        INTERMEDIATE_REPLY_TOOL_NAME,
        SESSION_THINK_TOOL_NAME,
    }
)


def report_from_record(message: dict, seen: set[str]) -> dict | None:
    """Один function_call или его результат. Чужое и служебное — None."""
    kind = message.get("type")
    if kind == _CALL:
        return _call_report(message, seen)
    if kind == _CALL_OUTPUT:
        return _output_report(message, seen)
    return None


class ToolFlow:
    """Хвост jsonl с момента создания. Повторный poll дочитывает только новое."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._offset = _file_size(path)
        self._pending = b""
        self._seen: set[str] = set()

    def poll(self) -> list[dict]:
        self._pending += _read_new(self._path, self)
        return _drain(self._pending, self._seen, self)


def _call_report(message: dict, seen: set[str]) -> dict | None:
    name = message.get("name")
    call_id = _call_id(message)
    if not isinstance(name, str) or name == "" or name in _HIDDEN:
        return None
    if call_id is None or call_id in seen:
        return None
    seen.add(call_id)
    return {
        "op": "open",
        "call_id": call_id,
        "name": name,
        "title": _title(name, message.get("arguments")),
    }


def _output_report(message: dict, seen: set[str]) -> dict | None:
    call_id = _call_id(message)
    if call_id is None or call_id not in seen:
        return None
    ok, _brief = parse_tool_result_status(message.get("output"))
    status = _FAILED if ok is False else _COMPLETED
    return {"op": "close", "call_id": call_id, "status": status}


def _title(name: str, arguments: Any) -> str:
    preview = brief_args_summary(arguments, tool_name=name)
    if preview == "":
        return name
    return f"{name}({preview})"


def _call_id(message: dict) -> str | None:
    call_id = message.get("call_id")
    if isinstance(call_id, str) and call_id != "":
        return call_id
    fallback = message.get("id")
    if isinstance(fallback, str) and fallback != "":
        return fallback
    return None


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _read_new(path: str, flow: ToolFlow) -> bytes:
    try:
        size = os.path.getsize(path)
        # Компакция переписывает jsonl короткой сводкой: старый offset за EOF,
        # seek+read даёт пусто, и карточки хода больше не появляются.
        if size < flow._offset:
            flow._offset = 0
            flow._pending = b""
        with open(path, "rb") as handle:
            handle.seek(flow._offset)
            chunk = handle.read()
            flow._offset = handle.tell()
    except OSError:
        return b""
    return chunk


def _drain(pending: bytes, seen: set[str], flow: ToolFlow) -> list[dict]:
    updates: list[dict] = []
    blob = pending
    while b"\n" in blob:
        raw, blob = blob.split(b"\n", 1)
        update = _from_line(raw, seen)
        if update is not None:
            updates.append(update)
    flow._pending = blob
    return updates


def _from_line(raw: bytes, seen: set[str]) -> dict | None:
    text = raw.decode("utf-8", errors="replace").strip()
    if text == "":
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    message = _message(record)
    if message is None:
        return None
    return report_from_record(message, seen)


def _message(record: Any) -> dict | None:
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if record.get("type") == _ENVELOPE and isinstance(payload, dict):
        return payload
    return record
