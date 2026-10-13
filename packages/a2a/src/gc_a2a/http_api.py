"""HTTP: карточка без JSON-RPC и POST ``/rpc``."""

from __future__ import annotations

import json
import logging
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import Response

from gc_a2a.errors import (
    RpcError,
    error_body,
    internal_error,
    method_not_found,
    push_not_supported,
    unauthorized,
    unsupported,
    version_not_supported,
)
from gc_a2a.facade import A2aFacade
from gc_a2a.jsonrpc import parse_call, version_ok

logger = logging.getLogger(__name__)

_JSON = "application/json"
_UNSUPPORTED = frozenset(
    {
        "SendStreamingMessage",
        "SubscribeToTask",
        "GetExtendedAgentCard",
    }
)
_PUSH = frozenset(
    {
        "CreateTaskPushNotificationConfig",
        "GetTaskPushNotificationConfig",
        "ListTaskPushNotificationConfigs",
        "DeleteTaskPushNotificationConfig",
    }
)


def create_app(facade: A2aFacade) -> FastAPI:
    app = FastAPI()
    app.state.facade = facade
    app.add_api_route(
        "/.well-known/agent-card.json", agent_card, methods=["GET"]
    )
    app.add_api_route("/rpc", rpc, methods=["POST"])
    return app


async def agent_card(request: Request) -> Response:
    return _json(request.app.state.facade.card())


async def rpc(request: Request) -> Response:
    facade: A2aFacade = request.app.state.facade
    request_id = None
    try:
        _authorize(request, facade.auth_token)
        call = parse_call(await request.body())
        request_id = call["id"]
        _require_version(request)
        result = await _dispatch(facade, call["method"], call.get("params"))
    except RpcError as exc:
        return _json(error_body(exc, request_id), exc.status)
    except Exception:
        logger.exception("a2a rpc")
        return _json(error_body(internal_error(), request_id))
    return _json({"jsonrpc": "2.0", "id": request_id, "result": result})


async def _dispatch(facade: A2aFacade, method: str, params: object):
    if method in _UNSUPPORTED:
        raise unsupported()
    if method in _PUSH:
        raise push_not_supported()
    if method == "SendMessage":
        return await facade.send_message(params)
    if method == "GetTask":
        return await facade.get_task(params)
    if method == "ListTasks":
        return await facade.list_tasks(params)
    if method == "CancelTask":
        return await facade.cancel_task(params)
    raise method_not_found()


def _authorize(request: Request, token: str) -> None:
    if token == "":
        return
    header = request.headers.get("authorization")
    if _bearer_ok(header, token):
        return
    raise unauthorized()


def _bearer_ok(header: str | None, token: str) -> bool:
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return False
    got = header[len("Bearer ") :]
    if len(got) != len(token):
        return False
    return secrets.compare_digest(got, token)


def _require_version(request: Request) -> None:
    present = "a2a-version" in request.headers
    if version_ok(
        request.headers.get("a2a-version"),
        request.query_params.get("A2A-Version"),
        header_present=present,
    ):
        return
    raise version_not_supported()


def _json(payload: dict, status_code: int = 200) -> Response:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Response(content=raw, status_code=status_code, media_type=_JSON)
