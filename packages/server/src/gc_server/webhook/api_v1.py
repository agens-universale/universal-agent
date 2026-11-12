"""API ``/v1`` задач — одинаковые ручки у вебхука и swarm.

Поверхность — исполнитель задач (``TaskBackend``): вебхук ведёт ход в
процессе, swarm — в песочнице. Ручки здесь только разбирают запрос и
собирают ответ.

| Ручка | Что |
|---|---|
| ``POST /v1/tasks`` | сообщение задаче; ``202 {status, system, task_id, message_seq}`` |
| ``GET /v1/tasks`` | список задач (``system``, ``limit``, ``cursor``) |
| ``GET /v1/tasks/{system}/{task_id}`` | статус и ``pending_messages`` |
| ``GET …/messages`` | история страницами (``cursor``, ``limit``) |
| ``POST …/cancel`` | отмена хода, ``202`` |
| ``GET …/events`` | SSE: новые записи истории и смена статуса (``cursor``) |

Курсор истории непрозрачен (``history_page``): после сжатия или пересоздания
треда ответ несёт ``reset`` и историю новой версии с начала.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from gc_core.threads.errors import InvalidCursor, OperationMismatch
from gc_core.threads.models import ThreadKey

from gc_server.surface import http_error, read_json_object
from gc_server.webhook.history_page import (
    HistoryPage,
    Position,
    decode_cursor,
    encode_cursor,
)
from gc_server.webhook.idempotency import (
    idempotency_key,
    message_id,
    operation_id,
    task_id_for_key,
)
from gc_server.webhook.signatures import SignatureError, verify

#: Статусы, после которых у задачи ничего не меняется без нового сообщения.
FINAL = frozenset({"completed", "failed", "cancelled"})
_LIMIT_MAX = 1000
_NOT_FOUND = {"status": "not_found"}


class TaskBackend(Protocol):
    """Исполнитель задач поверхности."""

    default_system: str
    events_poll_seconds: float
    #: Ключи подписи тела (``signing_secrets()``,
    #: ``signature_tolerance_seconds``) — секция webhook config_server.yml.
    signing: Any

    async def asubmit(
        self,
        key: ThreadKey,
        message: dict,
        *,
        op_id: str | None,
        trace_id: str | None,
        callback_url: str | None = None,
    ) -> dict: ...

    async def astatus(self, key: ThreadKey) -> dict | None: ...

    async def acancel(self, key: ThreadKey) -> dict | None: ...

    async def alist(
        self, *, system: str | None, limit: int, cursor: str | None
    ) -> tuple[list[dict], str | None]: ...

    async def amessages(
        self, key: ThreadKey, *, position: Position | None, limit: int
    ) -> HistoryPage | None: ...


@dataclass(frozen=True)
class TaskRequest:
    key: ThreadKey
    content: str
    trace_id: str | None
    body: dict
    callback_url: str | None = None


def parse_task_request(payload: dict, default_system: str) -> TaskRequest:
    """Тело ``POST /v1/tasks`` → задача и текст агенту; негодное — 400 до
    любой записи. Без ``message`` текстом агенту уходит всё тело JSON."""
    from gc_server.webhook.surface import (
        request_system,
        request_task_identifiers,
    )

    # Адрес обратного вызова — поле запроса, а не слова задачи.
    callback_url = payload.pop("callback_url", None)
    if callback_url is not None and not isinstance(callback_url, str):
        raise http_error(400, "callback_url must be a string")
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
        ThreadKey(system=system, thread=task_id),
        content,
        trace_id,
        payload,
        callback_url,
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
        await self._verify(request)
        payload = await read_json_object(request)
        key = self._idempotency_key(request)
        if key is not None and payload.get("task_id") is None:
            # Повтор по ключу без task_id — в ту же задачу, а не в новую.
            payload["task_id"] = task_id_for_key(key)
        task = parse_task_request(payload, self.backend.default_system)
        message = _user_message(task.content)
        if key is not None:
            message["id"] = message_id(key)
        try:
            submitted = await self.backend.asubmit(
                task.key,
                message,
                op_id=operation_id(key) if key is not None else None,
                trace_id=task.trace_id,
                callback_url=task.callback_url,
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

    def _idempotency_key(self, request: Request) -> str | None:
        """С подписью ключ — подписанный ``webhook-id``: неподписанный
        ``Idempotency-Key`` позволил бы повторить перехваченный запрос."""
        if self.backend.signing.signing_secrets():
            return request.headers.get("webhook-id")
        return idempotency_key(request.headers)

    async def _verify(self, request: Request) -> None:
        """Подпись тела, если у поверхности задан секрет; иначе — ничего."""
        secrets = self.backend.signing.signing_secrets()
        if not secrets:
            return
        try:
            verify(
                secrets,
                request.headers,
                await request.body(),
                now=int(time.time()),
                tolerance=self.backend.signing.signature_tolerance_seconds,
            )
        except SignatureError as exc:
            raise http_error(401, str(exc)) from None

    async def list_tasks(
        self,
        system: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ):
        if system is not None:
            system = self.key_of(system, "x").system
        try:
            tasks, next_cursor = await self.backend.alist(
                system=system, limit=_bounded(limit), cursor=cursor
            )
        except InvalidCursor:
            raise http_error(400, "invalid cursor") from None
        return {"tasks": tasks, "next_cursor": next_cursor}

    async def get_task(self, system: str, task_id: str):
        status = await self.backend.astatus(self.key_of(system, task_id))
        if status is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return status

    async def get_messages(
        self,
        system: str,
        task_id: str,
        cursor: str | None = None,
        limit: int = 100,
        after: str | None = None,
    ):
        limit = _bounded(limit)
        page = await self.backend.amessages(
            self.key_of(system, task_id),
            position=_position(cursor, after),
            limit=limit,
        )
        if page is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return {
            "messages": page.entries,
            "next_cursor": encode_cursor(page.position),
            "has_more": len(page.entries) == limit,
            "reset": page.reset,
        }

    async def cancel_task(self, system: str, task_id: str):
        cancelled = await self.backend.acancel(self.key_of(system, task_id))
        if cancelled is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return JSONResponse(status_code=202, content=cancelled)

    async def task_events(
        self,
        request: Request,
        system: str,
        task_id: str,
        cursor: str | None = None,
        after: str | None = None,
    ):
        key = self.key_of(system, task_id)
        # Переподключение EventSource присылает id последнего события.
        cursor = cursor or request.headers.get("last-event-id") or None
        position = _position(cursor, after)
        if await self.backend.astatus(key) is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return StreamingResponse(
            _events(self.backend, key, request, position=position),
            media_type="text/event-stream",
        )


async def _events(
    backend: TaskBackend,
    key: ThreadKey,
    request: Request,
    *,
    position: Position | None,
) -> AsyncIterator[str]:
    """Новые записи истории (``message``, ``id`` — курсор после неё),
    замена истории (``reset``: дальше — история новой версии с начала) и
    смена статуса (``status``) опросом хранилища; поток кончается на
    финальном статусе."""
    seen_status: Any = None
    while not await request.is_disconnected():
        page = await backend.amessages(
            key, position=position, limit=_LIMIT_MAX
        )
        if page is not None:
            if page.reset:
                yield _sse("reset", {})
            for entry in page.entries:
                position = Position(
                    page.position.generation,
                    page.position.history_version,
                    entry["seq"],
                )
                yield _sse("message", entry, event_id=encode_cursor(position))
            position = page.position
        status = await backend.astatus(key)
        state = (status or {}).get("status")
        if status is not None and state != seen_status:
            seen_status = state
            yield _sse("status", status)
        if state in FINAL:
            return
        await asyncio.sleep(backend.events_poll_seconds)


def _sse(event: str, data: Any, *, event_id: str | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    body = json.dumps(data, ensure_ascii=False)
    return f"{head}event: {event}\ndata: {body}\n\n"


def _position(cursor: str | None, after: str | None) -> Position | None:
    """Позиция истории из курсора; числовой ``after`` прежней формы и
    негодный курсор — 400."""
    if after is not None:
        raise http_error(400, "after is not supported: use cursor")
    if cursor is None:
        return None
    try:
        return decode_cursor(cursor)
    except ValueError:
        raise http_error(400, "invalid cursor") from None


def _bounded(limit: int) -> int:
    return max(1, min(int(limit), _LIMIT_MAX))


def _user_message(content: str) -> dict:
    from gc_server.webhook.surface import make_user_message

    return make_user_message(content)
