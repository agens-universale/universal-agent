"""Маршруты ``/v1`` этого релиза."""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from gc_chat.http_guard import install_http_guard
from gc_chat.model_route import AGENT_ID, models_body
from gc_chat.responses.errors import ApiError, error_payload
from gc_chat.responses.facade import ResponsesFacade
from gc_chat.responses.turn import TurnRun

_JSON = "application/json"


def create_app(facade: ResponsesFacade) -> FastAPI:
    """Приложение только с маршрутами Responses. Тесты ходят сюда."""
    app = FastAPI()
    app.state.responses_facade = facade
    install_http_guard(
        app, cors_origins=facade.cors_origins, bind_host=facade.bind_host
    )
    app.add_api_route("/v1/models", models, methods=["GET"])
    mount(app)
    return app


def mount(app: FastAPI) -> None:
    """Маршруты Responses на уже созданном приложении. ``/v1/models`` не трогает."""
    app.add_api_route("/v1/responses", create_response, methods=["POST"])
    app.add_api_route(
        "/v1/responses/{response_id}", read_response, methods=["GET"]
    )
    app.add_api_route(
        "/v1/responses/{response_id}/cancel",
        cancel_response,
        methods=["POST"],
    )


def _facade(request: Request) -> ResponsesFacade:
    return request.app.state.responses_facade


async def models(request: Request) -> Response:
    facade = _facade(request)
    denied = _unauthorized(request, facade.auth_token)
    if denied is not None:
        return denied
    return _json(models_body(facade.model_id))


async def create_response(request: Request) -> Response:
    facade = _facade(request)
    denied = _unauthorized(request, facade.auth_token)
    if denied is not None:
        return denied
    body, rejected = await _object_body(request)
    if rejected is not None:
        return rejected
    if body.get("model") != AGENT_ID:
        exc = ApiError(400, "invalid_request", "unknown model")
        return _json(error_payload(exc), 400)
    opened = await _open_turn(facade, body)
    if isinstance(opened, Response):
        return opened
    if isinstance(body, dict) and body.get("stream") is True:
        return StreamingResponse(
            opened.iter_sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
    return await _finished_body(opened, request)


async def read_response(response_id: str, request: Request) -> Response:
    facade = _facade(request)
    denied = _unauthorized(request, facade.auth_token)
    if denied is not None:
        return denied
    try:
        return _json(facade.get(response_id))
    except ApiError as exc:
        return _json(error_payload(exc), exc.status)


async def cancel_response(response_id: str, request: Request) -> Response:
    facade = _facade(request)
    denied = _unauthorized(request, facade.auth_token)
    if denied is not None:
        return denied
    try:
        return _json(await facade.cancel(response_id))
    except ApiError as exc:
        return _json(error_payload(exc), exc.status)


async def _open_turn(facade: ResponsesFacade, body: Any) -> TurnRun | Response:
    try:
        return await facade.open(body)
    except ApiError as exc:
        return _json(error_payload(exc), exc.status)
    except Exception as exc:
        err = ApiError(500, "server_error", str(exc) or "server error")
        return _json(error_payload(err), 500)


async def _finished_body(run: TurnRun, request: Request) -> Response:
    try:
        payload = await run.wait_client(request)
    except Exception:
        await run.request_stop()
        raise
    code = 500 if payload.get("status") == "failed" else 200
    return _json(payload, code)


def _unauthorized(request: Request, token: str) -> Response | None:
    if token == "":
        return None
    header = request.headers.get("authorization")
    if _bearer_ok(header, token):
        return None
    exc = ApiError(401, "invalid_api_key", "invalid api key")
    return _json(error_payload(exc), 401)


def _bearer_ok(header: str | None, token: str) -> bool:
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return False
    got = header[len("Bearer ") :]
    if len(got) != len(token):
        return False
    return secrets.compare_digest(got, token)


async def _object_body(request: Request) -> tuple[Any, Response | None]:
    try:
        body = await request.json()
    except ValueError:
        exc = ApiError(
            400, "invalid_request", "request body must be an object"
        )
        return None, _json(error_payload(exc), 400)
    if not isinstance(body, dict):
        exc = ApiError(
            400, "invalid_request", "request body must be an object"
        )
        return None, _json(error_payload(exc), 400)
    return body, None


def _json(payload: dict, status_code: int = 200) -> Response:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Response(content=raw, status_code=status_code, media_type=_JSON)
