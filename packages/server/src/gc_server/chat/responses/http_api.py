"""Маршруты ``/v1`` этого релиза."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from gc_server.chat.errors import ApiError, error_payload
from gc_server.chat.http_common import (
    authorized_body,
    json_response,
    unauthorized,
)
from gc_server.chat.http_guard import install_http_guard
from gc_server.chat.model_route import AGENT_ID, models_body
from gc_server.chat.responses.facade import ResponsesFacade
from gc_server.chat.responses.turn import TurnRun


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
    denied = unauthorized(request, facade.auth_token)
    if denied is not None:
        return denied
    return json_response(models_body(facade.model_id))


async def create_response(request: Request) -> Response:
    facade = _facade(request)
    body, rejected = await authorized_body(request, facade.auth_token)
    if rejected is not None:
        return rejected
    if body.get("model") != AGENT_ID:
        exc = ApiError(400, "invalid_request", "unknown model")
        return json_response(error_payload(exc), 400)
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
    denied = unauthorized(request, facade.auth_token)
    if denied is not None:
        return denied
    try:
        return json_response(await facade.get(response_id))
    except ApiError as exc:
        return json_response(error_payload(exc), exc.status)


async def cancel_response(response_id: str, request: Request) -> Response:
    facade = _facade(request)
    denied = unauthorized(request, facade.auth_token)
    if denied is not None:
        return denied
    try:
        return json_response(await facade.cancel(response_id))
    except ApiError as exc:
        return json_response(error_payload(exc), exc.status)


async def _open_turn(facade: ResponsesFacade, body: Any) -> TurnRun | Response:
    try:
        return await facade.open(body)
    except ApiError as exc:
        return json_response(error_payload(exc), exc.status)
    except Exception as exc:
        err = ApiError(500, "server_error", str(exc) or "server error")
        return json_response(error_payload(err), 500)


async def _finished_body(run: TurnRun, request: Request) -> Response:
    try:
        payload = await run.wait_client(request)
    except Exception:
        await run.request_stop()
        raise
    code = 500 if payload.get("status") == "failed" else 200
    return json_response(payload, code)
