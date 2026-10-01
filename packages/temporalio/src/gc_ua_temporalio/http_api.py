"""Remote API mounted in gc-server, protected by its existing authentication."""

import asyncio
import json
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError, RPCStatusCode

from gc_ua_temporalio.control_contracts import HumanReply, ReconcileRequest
from gc_ua_temporalio.native_operator_contracts import (
    NativeOperatorResolution,
    NativeOperatorResume,
)
from gc_ua_temporalio.session_contracts import UserMessage


async def api_call(call):
    try:
        return await call
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except WorkflowUpdateFailedError as exc:
        raise HTTPException(409, str(exc.cause)) from exc
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
        from gc_ua_temporalio.diagnostics import session_metrics

        if getattr(registry, "backend", None) == "temporal-native":
            return await api_call(registry.status(system, thread))
        return await api_call(session_metrics(registry, system, thread))

    @routes.get("/{system}/{thread}/status")
    async def status(system: str, thread: str):
        return await api_call(registry.status(system, thread))

    @routes.post("/{system}/{thread}/messages")
    async def message(system: str, thread: str, value: UserMessage):
        return await api_call(registry.start_or_send(system, thread, value))

    _mount_views(registry, routes)

    @routes.post("/{system}/{thread}/answer")
    async def answer(system: str, thread: str, value: HumanReply):
        client = await registry.client()
        return await api_call(
            client.answer_human(registry.workflow_id(system, thread), value)
        )

    @routes.post("/{system}/{thread}/stop")
    async def stop(system: str, thread: str, value: StopRequest):
        client = await registry.client()
        return await api_call(
            client.stop_turn(
                registry.workflow_id(system, thread), value.turn_id
            )
        )

    @routes.post("/{system}/{thread}/close")
    async def close(system: str, thread: str):
        client = await registry.client()
        await api_call(
            client.close_session(registry.workflow_id(system, thread))
        )
        return {"closing": True}

    _mount_operator_routes(registry, routes)

    @routes.post("/{system}/{thread}/reconcile")
    async def reconcile(system: str, thread: str, value: ReconcileRequest):
        client = await registry.client()
        return await api_call(
            client.reconcile_operation(
                registry.workflow_id(system, thread), value
            )
        )

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
                **(
                    {"token": token}
                    if getattr(registry, "backend", None) == "temporal-native"
                    else {}
                ),
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


def _mount_operator_routes(registry, routes):
    @routes.post("/{system}/{thread}/operations/inspect")
    async def inspect_operation(system: str, thread: str):
        client = await _native_operator_client(registry)
        return await api_call(
            client.inspect_operation(registry.workflow_id(system, thread))
        )

    @routes.post("/{system}/{thread}/operations/resume")
    async def resume_operation(
        system: str, thread: str, value: NativeOperatorResume
    ):
        client = await _native_operator_client(registry)
        return await api_call(
            client.resume_operation(
                registry.workflow_id(system, thread),
                value.operation_id,
                value.digest,
            )
        )

    @routes.post("/{system}/{thread}/operations/resolve")
    async def resolve_operation(
        system: str, thread: str, value: NativeOperatorResolution
    ):
        client = await _native_operator_client(registry)
        return await api_call(
            client.resolve_operation(
                registry.workflow_id(system, thread), value
            )
        )


async def _native_operator_client(registry):
    if getattr(registry, "backend", None) != "temporal-native":
        raise HTTPException(
            400, "Native operator API requires temporal-native backend"
        )
    return await registry.client()
