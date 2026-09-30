"""Вызов тула в треде → короткая карточка между прозой.

Живой ход дописывает jsonl, пока stream() молчит на исполнении тула.
Хвост файла читается отдельно и не повторяет строки, которые уже были
до этого prompt.

Вызовы внутри субагента лежат не в треде, а в sidecar
``data_subagents_*``. Для клиента ACP это те же действия агента: та же
карточка ``tool_call``, без отдельной вложенности.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
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
_SIDECAR_CALL = "call"
_SIDECAR_DONE = frozenset({"result", "error"})
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


def report_from_record(
    message: dict, seen: set[str], finished: set[str] | None = None
) -> dict | None:
    """Один function_call или его результат. Чужое и служебное — None."""
    kind = message.get("type")
    if kind == _CALL:
        return _call_report(message, seen)
    if kind == _CALL_OUTPUT:
        return _output_report(message, seen, finished)
    return None


def report_from_sidecar(
    record: dict, seen: set[str], finished: set[str] | None = None
) -> dict | None:
    """Запись sidecar → та же карточка, что у тула родителя.

    ``call_id`` внутри субагента свой, и он может совпасть с id родителя.
    В карточке id составной: родитель и вызов через двоеточие.
    """
    kind = record.get("kind")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    message = _scoped_payload(payload, record.get("parent_call_id"))
    if kind == _SIDECAR_CALL:
        return _call_report(message, seen)
    if kind not in _SIDECAR_DONE:
        return None
    update = _output_report(message, seen, finished)
    if update is not None and kind == "error":
        return {**update, "status": _FAILED}
    return update


class ToolFlow:
    """Хвост jsonl с момента создания. Повторный poll дочитывает только новое.

    ``sidecar_path`` — файл вызовов субагента. Оба хвоста за один poll
    сливаются по ``ts``, чтобы тул внутри субагента встал между стартом
    субагента и его результатом, а не после.
    """

    def __init__(self, path: str, *, sidecar_path: str | None = None) -> None:
        self._parent = _Tail(path)
        self._sidecar = _Tail(sidecar_path or "")
        self._seen: set[str] = set()
        self._finished: set[str] = set()

    def poll(self) -> list[dict]:
        batch = _stamped(self._parent.read_lines(), "parent")
        batch.extend(
            _stamped(self._sidecar.read_lines(), "sidecar", len(batch))
        )
        batch.sort()
        updates: list[dict] = []
        for _stamp, _order, kind, raw in batch:
            update = _decode(kind, raw, self._seen, self._finished)
            if update is not None:
                updates.append(update)
        return updates


def _call_report(message: dict, seen: set[str]) -> dict | None:
    name = message.get("name")
    call_id = _call_id(message)
    if not isinstance(name, str) or name == "" or name in _HIDDEN:
        return None
    if call_id is None or call_id in seen:
        return None
    seen.add(call_id)
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "title": _title(name, message.get("arguments")),
        "status": _IN_PROGRESS,
    }


def _output_report(
    message: dict, seen: set[str], finished: set[str] | None = None
) -> dict | None:
    call_id = _call_id(message)
    if call_id is None or call_id not in seen:
        return None
    if finished is not None and call_id in finished:
        return None
    if finished is not None:
        finished.add(call_id)
    ok, _brief = parse_tool_result_status(message.get("output"))
    status = _FAILED if ok is False else _COMPLETED
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call_id,
        "status": status,
    }


def _scoped_payload(payload: dict, parent: Any) -> dict:
    call_id = payload.get("call_id")
    if (
        not isinstance(parent, str)
        or parent == ""
        or not isinstance(call_id, str)
        or call_id == ""
    ):
        return payload
    scoped = dict(payload)
    scoped["call_id"] = f"{parent}:{call_id}"
    return scoped


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


@dataclass
class _Tail:
    """Непрочитанный хвост одного jsonl. Файл может появиться позже."""

    path: str
    offset: int = 0
    pending: bytes = b""

    def __post_init__(self) -> None:
        self.offset = _file_size(self.path)

    def read_lines(self) -> list[bytes]:
        if self.path == "":
            return []
        self.pending += _read_new(self)
        lines, self.pending = _complete_lines(self.pending)
        return lines


def _file_size(path: str) -> int:
    if path == "":
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _read_new(tail: _Tail) -> bytes:
    try:
        size = os.path.getsize(tail.path)
        # Компакция переписывает jsonl короткой сводкой: старый offset за EOF,
        # seek+read даёт пусто, и карточки хода больше не появляются.
        if size < tail.offset:
            tail.offset = 0
            tail.pending = b""
        with open(tail.path, "rb") as handle:
            handle.seek(tail.offset)
            chunk = handle.read()
            tail.offset = handle.tell()
    except OSError:
        return b""
    return chunk


def _complete_lines(pending: bytes) -> tuple[list[bytes], bytes]:
    lines: list[bytes] = []
    blob = pending
    while b"\n" in blob:
        raw, blob = blob.split(b"\n", 1)
        lines.append(raw)
    return lines, blob


def _stamped(
    lines: list[bytes], kind: str, start: int = 0
) -> list[tuple[str, int, str, bytes]]:
    return [
        (_stamp(raw), start + index, kind, raw)
        for index, raw in enumerate(lines)
    ]


def _stamp(raw: bytes) -> str:
    record = _json_object(raw)
    if record is None:
        return ""
    ts = record.get("ts")
    if isinstance(ts, str):
        return ts
    return ""


def _decode(
    kind: str, raw: bytes, seen: set[str], finished: set[str]
) -> dict | None:
    record = _json_object(raw)
    if record is None:
        return None
    if kind == "sidecar":
        return report_from_sidecar(record, seen, finished)
    message = _message(record)
    if message is None:
        return None
    return report_from_record(message, seen, finished)


def _json_object(raw: bytes) -> dict | None:
    text = raw.decode("utf-8", errors="replace").strip()
    if text == "":
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(record, dict):
        return record
    return None


def _message(record: Any) -> dict | None:
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if record.get("type") == _ENVELOPE and isinstance(payload, dict):
        return payload
    return record
