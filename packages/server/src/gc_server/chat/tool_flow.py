"""Хвост истории треда и трассы субагентов → короткая карточка тула.

Один отбор у Responses и Chat Completions: имя, заголовок, статус.
Сырые ``arguments`` и тело ``output`` в карточку не попадают.
``turn_finish``, ``turn_ask_human``, ``turn_intermediate_reply`` и
``session_think`` наружу не выходят. Проза субагента, служебный user
и метка сводки — тоже.
"""

from __future__ import annotations

from typing import Any

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
from gc_core.threads.facade import Cursor, Threads
from gc_core.threads.models import ThreadKey

_CALL = "function_call"
_CALL_OUTPUT = "function_call_output"
_HISTORY = "messages"
_SUBAGENTS = "subagent_events"
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


def report_from_record(
    message: dict, seen: set[str], *, root: str | None = None
) -> dict | None:
    """Один function_call или его результат. Чужое и служебное — None.

    ``root`` — cwd хода: пути в подписи вызова от него."""
    kind = message.get("type")
    if kind == _CALL:
        return _call_report(message, seen, root=root)
    if kind == _CALL_OUTPUT:
        return _output_report(message, seen)
    return None


class ToolFlow:
    """Хвост треда с момента :meth:`start`. Повторный poll дочитывает новое.

    Рядом с историей читается трасса субагента: вложенный тул — отдельная
    карточка, ``call_id`` склеен с ``parent_call_id``. Сжатие истории посреди
    хода начинает её чтение заново; показанные карточки не повторяются.
    ``root`` — cwd хода: пути в подписях вызовов от него.
    """

    def __init__(
        self, threads: Threads, key: ThreadKey, *, root: str | None = None
    ) -> None:
        self._threads = threads
        self._key = key
        self._cursor: Cursor | None = None
        self._seen: set[str] = set()
        self._root = root

    @classmethod
    async def start(
        cls, threads: Threads, key: ThreadKey, *, root: str | None = None
    ) -> ToolFlow:
        flow = cls(threads, key, root=root)
        flow._cursor = (
            await threads.tail(key, (_HISTORY, _SUBAGENTS), None)
        ).cursor
        return flow

    async def poll(self) -> list[dict]:
        batch = await self._threads.tail(
            self._key, (_HISTORY, _SUBAGENTS), self._cursor
        )
        self._cursor = batch.cursor
        reports: list[dict] = []
        for stream, entry in batch.entries:
            if stream == _SUBAGENTS:
                report = _from_sidecar(entry.data, self._seen, self._root)
            else:
                report = report_from_record(
                    entry.data, self._seen, root=self._root
                )
            if report is not None:
                reports.append(report)
        return reports


def _card_id(call_id: str, parent: str | None) -> str:
    if parent is None:
        return call_id
    return parent + "/" + call_id


def _call_report(
    message: dict,
    seen: set[str],
    *,
    parent: str | None = None,
    root: str | None = None,
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
        "title": _title(name, message.get("arguments"), root),
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


def _title(name: str, arguments: Any, root: str | None) -> str:
    preview = tool_args_summary(name, arguments, root=root)
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


def _from_sidecar(
    record: dict, seen: set[str], root: str | None
) -> dict | None:
    payload = record.get("payload")
    parent = record.get("parent_call_id")
    if not isinstance(payload, dict):
        return None
    if not isinstance(parent, str) or parent == "":
        return None
    kind = record.get("kind")
    if kind == _SIDECAR_CALL:
        return _call_report(payload, seen, parent=parent, root=root)
    if kind == _SIDECAR_RESULT:
        return _output_report(payload, seen, parent=parent)
    if kind == _SIDECAR_ERROR:
        return _output_report(payload, seen, parent=parent, failed=True)
    return None
