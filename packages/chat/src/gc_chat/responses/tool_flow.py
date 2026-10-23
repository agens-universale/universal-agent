"""Хвост jsonl треда и sidecar субагента → короткая карточка тула.

Сырые ``arguments`` и тело ``output`` в карточку не попадают.
``turn_finish``, ``turn_ask_human``, ``turn_intermediate_reply`` и
``session_think`` наружу не выходят. Проза субагента, служебный user
и метка сводки — тоже.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    FINISH_TOOL_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
    SESSION_THINK_TOOL_NAME,
)
from gc_core.execution.progress import (
    parse_tool_result_status,
    tool_args_summary,
)

_CALL = "function_call"
_CALL_OUTPUT = "function_call_output"
_ENVELOPE = "message"
_COMPLETED = "completed"
_FAILED = "failed"
_SIDECAR_CALL = "call"
_SIDECAR_RESULT = "result"
_SIDECAR_ERROR = "error"
_HIDDEN = frozenset(
    {
        FINISH_TOOL_NAME,
        ASK_HUMAN_TOOL_NAME,
        INTERMEDIATE_REPLY_TOOL_NAME,
        SESSION_THINK_TOOL_NAME,
    }
)

_LineParser = Callable[[bytes, set[str]], dict | None]


def report_from_record(message: dict, seen: set[str]) -> dict | None:
    """Один function_call или его результат. Чужое и служебное — None."""
    kind = message.get("type")
    if kind == _CALL:
        return _call_report(message, seen)
    if kind == _CALL_OUTPUT:
        return _output_report(message, seen)
    return None


class _Tail:
    """Смещение и недочитанный хвост одного jsonl."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.offset = _file_size(path)
        self.pending = b""


class ToolFlow:
    """Хвост jsonl с момента создания. Повторный poll дочитывает только новое.

    Рядом с тредом читается sidecar субагента: вложенный тул — отдельная
    карточка, ``call_id`` склеен с ``parent_call_id``.
    """

    def __init__(self, path: str) -> None:
        self._thread = _Tail(path)
        self._sidecar = _Tail(_sidecar_path(path))
        self._seen: set[str] = set()

    def poll(self) -> list[dict]:
        reports = _poll_tail(self._thread, self._seen, _from_thread_line)
        reports.extend(
            _poll_tail(self._sidecar, self._seen, _from_sidecar_line)
        )
        return reports


def _sidecar_path(thread_path: str) -> str:
    """``data_{system}_{thread}.jsonl`` → ``data_subagents_...``.

    Имя совпадает с ``subagent_trace_path``. Прочий jsonl в тестах
    получает соседний ``*.subagents.jsonl``.
    """
    directory, name = os.path.split(thread_path)
    prefix = "data_"
    nested = "data_subagents_"
    if name.startswith(prefix) and not name.startswith(nested):
        sidecar_name = nested + name[len(prefix) :]
    else:
        root, ext = os.path.splitext(name)
        sidecar_name = root + ".subagents" + ext
    return os.path.join(directory, sidecar_name)


def _card_id(call_id: str, parent: str | None) -> str:
    if parent is None:
        return call_id
    return parent + "/" + call_id


def _call_report(
    message: dict, seen: set[str], *, parent: str | None = None
) -> dict | None:
    name = message.get("name")
    call_id = _call_id(message)
    if not isinstance(name, str) or name == "" or name in _HIDDEN:
        return None
    if call_id is None:
        return None
    card_id = _card_id(call_id, parent)
    if card_id in seen:
        return None
    seen.add(card_id)
    report = {
        "op": "open",
        "call_id": card_id,
        "name": name,
        "title": _title(name, message.get("arguments")),
    }
    if parent is not None:
        report["parent_call_id"] = parent
    return report


def _output_report(
    message: dict,
    seen: set[str],
    *,
    parent: str | None = None,
    failed: bool = False,
) -> dict | None:
    call_id = _call_id(message)
    if call_id is None:
        return None
    card_id = _card_id(call_id, parent)
    if card_id not in seen:
        return None
    if failed:
        status = _FAILED
    else:
        ok, _brief = parse_tool_result_status(message.get("output"))
        status = _FAILED if ok is False else _COMPLETED
    return {"op": "close", "call_id": card_id, "status": status}


def _title(name: str, arguments: Any) -> str:
    preview = tool_args_summary(name, arguments)
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


def _poll_tail(tail: _Tail, seen: set[str], parse: _LineParser) -> list[dict]:
    tail.pending += _read_new(tail)
    updates: list[dict] = []
    blob = tail.pending
    while b"\n" in blob:
        raw, blob = blob.split(b"\n", 1)
        update = parse(raw, seen)
        if update is not None:
            updates.append(update)
    tail.pending = blob
    return updates


def _from_thread_line(raw: bytes, seen: set[str]) -> dict | None:
    record = _json_object(raw)
    if record is None:
        return None
    message = _message(record)
    if message is None:
        return None
    return report_from_record(message, seen)


def _from_sidecar_line(raw: bytes, seen: set[str]) -> dict | None:
    record = _json_object(raw)
    if record is None:
        return None
    payload = record.get("payload")
    parent = record.get("parent_call_id")
    if not isinstance(payload, dict):
        return None
    if not isinstance(parent, str) or parent == "":
        return None
    kind = record.get("kind")
    if kind == _SIDECAR_CALL:
        return _call_report(payload, seen, parent=parent)
    if kind == _SIDECAR_RESULT:
        return _output_report(payload, seen, parent=parent)
    if kind == _SIDECAR_ERROR:
        return _output_report(payload, seen, parent=parent, failed=True)
    return None


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


def _message(record: dict) -> dict | None:
    payload = record.get("payload")
    if record.get("type") == _ENVELOPE and isinstance(payload, dict):
        return payload
    return record
