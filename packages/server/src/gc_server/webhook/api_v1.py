"""API ``/v1`` задач — одинаковые ручки у вебхука и swarm.

Поверхность — исполнитель задач (``TaskBackend``): вебхук ведёт ход в
процессе, swarm — в песочнице. Ручки здесь только разбирают запрос и
собирают ответ.

| Ручка | Что |
|---|---|
| ``POST /v1/tasks`` | сообщение задаче; ``202 {status, system, task_id, message_seq}`` |
| ``GET /v1/tasks`` | список задач (``system``, ``limit``, ``cursor``) |
| ``GET /v1/tasks/{system}/{task_id}`` | статус и ``pending_messages`` |
| ``GET …/messages`` | история страницами (``after``, ``limit``) |
| ``POST …/cancel`` | отмена хода, ``202`` |
| ``GET …/events`` | SSE: новые записи истории и смена статуса |
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from gc_core.threads.errors import OperationMismatch
from gc_core.threads.models import ThreadKey

from gc_server.surface import http_error, read_json_object
from gc_server.webhook.idempotency import (
    idempotency_key,
    message_id,
    operation_id,
)

#: Статусы, после которых у задачи ничего не меняется без нового сообщения.
FINAL = frozenset({"completed", "failed", "cancelled"})
_LIMIT_MAX = 1000
_NOT_FOUND = {"status": "not_found"}


class TaskBackend(Protocol):
    """Исполнитель задач поверхности."""

    default_system: str
    events_poll_seconds: float

    async def asubmit(
        self,
        key: ThreadKey,
        message: dict,
        *,
        op_id: str | None,
        trace_id: str | None,
    ) -> dict: ...

    async def astatus(self, key: ThreadKey) -> dict | None: ...

    async def acancel(self, key: ThreadKey) -> dict | None: ...

    async def alist(
        self, *, system: str | None, limit: int, cursor: str | None
    ) -> tuple[list[dict], str | None]: ...

    async def amessages(
        self, key: ThreadKey, *, after: int, limit: int
    ) -> list[dict] | None: ...


@dataclass(frozen=True)
class TaskRequest:
    key: ThreadKey
    content: str
    trace_id: str | None
    body: dict


def parse_task_request(payload: dict, default_system: str) -> TaskRequest:
    """Тело ``POST /v1/tasks`` → задача и текст агенту; негодное — 400 до
    любой записи. Без ``message`` текстом агенту уходит всё тело JSON."""
    from gc_server.webhook.surface import (
        request_system,
        request_task_identifiers,
    )

    message = payload.get("message")
    if message is not None and (not isinstance(message, str) or not message):
        raise http_error(400, "message must be a non-empty string")
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise http_error(400, "metadata must be an object")
    task_id, trace_id = request_task_identifiers(payload)
    system = request_system(payload, default_system)
    if message is None:
        content = json.dumps(payload, ensure_ascii=False)
    elif metadata:
        content = (
            message
            + "\n\nmetadata: "
            + json.dumps(metadata, ensure_ascii=False)
        )
    else:
        content = message
    return TaskRequest(
        ThreadKey(system=system, thread=task_id), content, trace_id, payload
    )


def build_v1_routes(app: FastAPI, backend: TaskBackend) -> None:
    routes = _Routes(backend)
    app.add_api_route("/v1/tasks", routes.create_task, methods=["POST"])
    app.add_api_route("/v1/tasks", routes.list_tasks, methods=["GET"])
    base = "/v1/tasks/{system}/{task_id}"
    app.add_api_route(base, routes.get_task, methods=["GET"])
    app.add_api_route(base + "/messages", routes.get_messages, methods=["GET"])
    app.add_api_route(base + "/cancel", routes.cancel_task, methods=["POST"])
    app.add_api_route(base + "/events", routes.task_events, methods=["GET"])


class _Routes:
    """Ручки ``/v1`` поверх исполнителя задач поверхности."""

    def __init__(self, backend: TaskBackend) -> None:
        self.backend = backend

    @staticmethod
    def key_of(system: str, task_id: str) -> ThreadKey:
        from gc_server.webhook.surface import path_key

        sys_norm, task_norm = path_key(system, task_id)
        return ThreadKey(system=sys_norm, thread=task_norm)

    async def create_task(self, request: Request):
        payload = await read_json_object(request)
        task = parse_task_request(payload, self.backend.default_system)
        message = _user_message(task.content)
        key = idempotency_key(request.headers)
        if key is not None:
            message["id"] = message_id(key)
        try:
            submitted = await self.backend.asubmit(
                task.key,
                message,
                op_id=operation_id(key) if key is not None else None,
                trace_id=task.trace_id,
            )
        except OperationMismatch:
            raise http_error(
                422, "idempotency key reused with another request body"
            ) from None
        return JSONResponse(
            status_code=202,
            content={
                **submitted,
                "system": task.key.system,
                "task_id": task.key.thread,
            },
        )

    async def list_tasks(
        self,
        system: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ):
        if system is not None:
            system = self.key_of(system, "x").system
        tasks, next_cursor = await self.backend.alist(
            system=system, limit=_bounded(limit), cursor=cursor
        )
        return {"tasks": tasks, "next_cursor": next_cursor}

    async def get_task(self, system: str, task_id: str):
        status = await self.backend.astatus(self.key_of(system, task_id))
        if status is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return status

    async def get_messages(
        self, system: str, task_id: str, after: int = 0, limit: int = 100
    ):
        limit = _bounded(limit)
        entries = await self.backend.amessages(
            self.key_of(system, task_id), after=max(0, after), limit=limit
        )
        if entries is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        next_after = entries[-1]["seq"] if len(entries) == limit else None
        return {"messages": entries, "next_after": next_after}

    async def cancel_task(self, system: str, task_id: str):
        cancelled = await self.backend.acancel(self.key_of(system, task_id))
        if cancelled is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return JSONResponse(status_code=202, content=cancelled)

    async def task_events(
        self, request: Request, system: str, task_id: str, after: int = 0
    ):
        key = self.key_of(system, task_id)
        if await self.backend.astatus(key) is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return StreamingResponse(
            _events(self.backend, key, request, after=max(0, after)),
            media_type="text/event-stream",
        )


async def _events(
    backend: TaskBackend, key: ThreadKey, request: Request, *, after: int
) -> AsyncIterator[str]:
    """Новые записи истории (``message``) и смена статуса (``status``)
    опросом хранилища; поток кончается на финальном статусе."""
    seen_status: Any = None
    while not await request.is_disconnected():
        entries = await backend.amessages(key, after=after, limit=_LIMIT_MAX)
        for entry in entries or []:
            after = entry["seq"]
            yield _sse("message", entry)
        status = await backend.astatus(key)
        state = (status or {}).get("status")
        if status is not None and state != seen_status:
            seen_status = state
            yield _sse("status", status)
        if state in FINAL:
            return
        await asyncio.sleep(backend.events_poll_seconds)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _bounded(limit: int) -> int:
    return max(1, min(int(limit), _LIMIT_MAX))


def _user_message(content: str) -> dict:
    from gc_server.webhook.surface import make_user_message

    return make_user_message(content)
