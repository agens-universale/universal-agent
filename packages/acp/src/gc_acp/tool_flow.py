"""Вызов тула в треде → короткая карточка между прозой.

Живой ход дописывает историю треда, пока stream() молчит на исполнении
тула. Хвост истории читается отдельно (``Threads.tail``) и не повторяет
записи, которые уже были до этого prompt.

Вызовы внутри субагента лежат не в истории, а в потоке ``subagent_events``.
Для клиента ACP это те же действия агента: та же карточка ``tool_call``, без
отдельной вложенности.
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
    brief_args_summary,
    parse_tool_result_status,
)
from gc_core.threads.facade import Cursor, Threads
from gc_core.threads.models import StreamEntry, ThreadKey

_CALL = "function_call"
_CALL_OUTPUT = "function_call_output"
_HISTORY = "messages"
_SUBAGENTS = "subagent_events"
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
    """Хвост треда с момента :meth:`start`. Повторный poll дочитывает новое.

    История и вызовы субагента за один poll сливаются по ``ts``, чтобы тул
    внутри субагента встал между стартом субагента и его результатом, а не
    после. Сжатие истории посреди хода начинает её чтение заново; уже
    показанные карточки не повторяются.
    """

    def __init__(self, threads: Threads, key: ThreadKey) -> None:
        self._threads = threads
        self._key = key
        self._cursor: Cursor | None = None
        self._seen: set[str] = set()
        self._finished: set[str] = set()

    @classmethod
    async def start(cls, threads: Threads, key: ThreadKey) -> ToolFlow:
        flow = cls(threads, key)
        flow._cursor = (
            await threads.tail(key, (_HISTORY, _SUBAGENTS), None)
        ).cursor
        return flow

    async def poll(self) -> list[dict]:
        batch = await self._threads.tail(
            self._key, (_HISTORY, _SUBAGENTS), self._cursor
        )
        self._cursor = batch.cursor
        ordered = sorted(
            (entry.ts, order, stream, entry)
            for order, (stream, entry) in enumerate(batch.entries)
        )
        updates: list[dict] = []
        for _ts, _order, stream, entry in ordered:
            update = _decode(stream, entry, self._seen, self._finished)
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


def _decode(
    stream: str, entry: StreamEntry, seen: set[str], finished: set[str]
) -> dict | None:
    if stream == _SUBAGENTS:
        record = {"ts": entry.ts, "seq": entry.seq, **entry.data}
        return report_from_sidecar(record, seen, finished)
    return report_from_record(entry.data, seen, finished)
