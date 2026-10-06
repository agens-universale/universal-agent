"""Remote API mounted in gc-server, protected by its existing authentication."""

import asyncio
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException
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
            client, await registry.lookup_workflow_id(system, thread)
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
