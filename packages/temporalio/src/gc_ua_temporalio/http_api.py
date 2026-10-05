"""Remote API mounted in gc-server, protected by its existing authentication."""

import asyncio
import json
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from temporalio.client import (
    WorkflowQueryFailedError,
    WorkflowQueryRejectedError,
    WorkflowUpdateFailedError,
)
from temporalio.service import RPCError, RPCStatusCode

from gc_ua_temporalio.native_contracts import HumanReply, UserMessage


async def api_call(call):
    try:
        return await call
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except WorkflowUpdateFailedError as exc:
        raise HTTPException(409, str(exc.cause)) from exc
    except (WorkflowQueryFailedError, WorkflowQueryRejectedError) as exc:
        # Сессия ещё не готова отвечать или уже закрыта — не сбой сервера.
        raise HTTPException(409, str(exc)) from exc
    except RPCError as exc:
        code = 404 if exc.status == RPCStatusCode.NOT_FOUND else 503
        raise HTTPException(code, "Temporal request unavailable") from exc


@dataclass
class StopRequest:
    turn_id: str


def router(registry):
    routes = APIRouter(prefix="/temporal")

    @routes.get("/{system}/{thread}/metrics")
    async def metrics(system: str, thread: str):
        return await api_call(registry.status(system, thread))

    @routes.get("/{system}/{thread}/status")
    async def status(system: str, thread: str):
        return await api_call(registry.status(system, thread))

    @routes.post("/{system}/{thread}/messages")
    async def message(system: str, thread: str, value: UserMessage):
        return await api_call(registry.start_or_send(system, thread, value))

    _mount_views(registry, routes)

    async def control(system, thread, action):
        # workflow_id() проверяет ID: ValueError → 400, а не 500.
        client = await registry.client()
        return await action(
            client, await registry.aworkflow_id(system, thread)
        )

    @routes.post("/{system}/{thread}/answer")
    async def answer(system: str, thread: str, value: HumanReply):
        return await api_call(
            control(
                system,
                thread,
                lambda client, workflow_id: client.answer_human(
                    workflow_id, value
                ),
            )
        )

    @routes.post("/{system}/{thread}/stop")
    async def stop(system: str, thread: str, value: StopRequest):
        return await api_call(
            control(
                system,
                thread,
                lambda client, workflow_id: client.stop_turn(
                    workflow_id, value.turn_id
                ),
            )
        )

    @routes.post("/{system}/{thread}/close")
    async def close(system: str, thread: str):
        await api_call(
            control(
                system,
                thread,
                lambda client, workflow_id: client.close_session(workflow_id),
            )
        )
        return {"closing": True}

    @routes.patch("/{system}/{thread}/ui")
    async def ui(system: str, thread: str, value: dict):
        return await api_call(
            asyncio.to_thread(registry.views.patch_ui, system, thread, value)
        )

    return routes


def _mount_views(registry, routes):
    @routes.get("/{system}/{thread}/history")
    async def history(
        system: str,
        thread: str,
        after: int = 0,
        limit: int = 100,
        token: str | None = None,
    ):
        return await api_call(
            asyncio.to_thread(
                registry.views.history,
                system,
                thread,
                after=after,
                limit=limit,
                token=token,
            )
        )

    @routes.get("/{system}/{thread}/events")
    async def events(
        system: str, thread: str, after: int = 0, limit: int = 200
    ):
        return await api_call(
            asyncio.to_thread(
                registry.views.events, system, thread, after=after, limit=limit
            )
        )

    @routes.get("/{system}/{thread}/stream")
    async def stream(
        system: str, thread: str, request: Request, after: int = 0
    ):
        try:
            cursor = int(request.headers.get("last-event-id", after))
            if cursor < 0:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(400, "Invalid event cursor") from exc

        async def body():
            nonlocal cursor
            while not await request.is_disconnected():
                values = await asyncio.to_thread(
                    registry.views.events, system, thread, after=cursor
                )
                for event in values:
                    cursor = event["cursor"]
                    yield f"id: {cursor}\nevent: {event['kind']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not values:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(1)

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
