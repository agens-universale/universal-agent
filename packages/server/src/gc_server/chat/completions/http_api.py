"""Маршруты ``/v1/models`` и ``/v1/chat/completions``."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from gc_server.chat.completions.facade import CompletionsFacade
from gc_server.chat.completions.sse import (
    ChunkEncoder,
    completion_object,
    encode_data,
    encode_done,
)
from gc_server.chat.completions.turn import TurnRun
from gc_server.chat.direct_completions import DirectCompletions
from gc_server.chat.errors import ApiError, error_payload
from gc_server.chat.http_common import (
    authorized_body,
    disconnected,
    json_response,
    unauthorized,
)
from gc_server.chat.http_guard import install_http_guard
from gc_server.chat.model_route import completions_route, models_body

_POLL_S = 0.05


def create_app(facade: CompletionsFacade) -> FastAPI:
    """Приложение только с маршрутом Chat Completions. Тесты ходят сюда."""
    app = FastAPI()
    app.state.completions_facade = facade
    install_http_guard(
        app, cors_origins=facade.cors_origins, bind_host=facade.bind_host
    )
    app.add_api_route("/v1/models", models, methods=["GET"])
    mount(app)
    return app


def mount(app: FastAPI) -> None:
    """Маршрут Chat Completions. ``/v1/models`` регистрирует общее приложение."""
    app.add_api_route(
        "/v1/chat/completions", create_completion, methods=["POST"]
    )


def _facade(request: Request) -> CompletionsFacade:
    return request.app.state.completions_facade


async def models(request: Request) -> Response:
    facade = _facade(request)
    denied = unauthorized(request, facade.auth_token)
    if denied is not None:
        return denied
    return json_response(models_body(facade.model_id))


async def create_completion(request: Request) -> Response:
    facade = _facade(request)
    body, rejected = await authorized_body(request, facade.auth_token)
    if rejected is not None:
        return rejected
    route = completions_route(body.get("model"), facade.model_id)
    if route is None:
        exc = ApiError(400, "invalid_request", "unknown model")
        return json_response(error_payload(exc), 400)
    if route == "direct":
        direct = DirectCompletions(
            settings=facade._settings, auth_connector=facade._auth
        )
        return await direct.forward(body, request)
    opened = await _open_turn(facade, body)
    if isinstance(opened, Response):
        return opened
    if isinstance(body, dict) and body.get("stream") is True:
        return await _stream_response(opened, request)
    return await _finished_body(opened, request)


async def _open_turn(
    facade: CompletionsFacade, body: Any
) -> TurnRun | Response:
    try:
        return await facade.open(body)
    except ApiError as exc:
        return json_response(error_payload(exc), exc.status)
    except Exception as exc:
        err = ApiError(500, "server_error", str(exc) or "server error")
        return json_response(error_payload(err), 500)


async def _stream_response(run: TurnRun, request: Request) -> Response:
    first = await _pull_until(run, request)
    if first is None:
        return Response(status_code=204)
    kind, payload = first
    if kind == "error":
        return json_response(payload, 500)
    if kind == "cancelled":
        return Response(status_code=204)
    return StreamingResponse(
        _iter_sse(run, first),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


async def _finished_body(run: TurnRun, request: Request) -> Response:
    parts: list[str] = []
    while True:
        item = await _pull_until(run, request)
        if item is None:
            return Response(status_code=204)
        kind, payload = item
        if kind == "content":
            parts.append(payload)
            continue
        if kind == "error":
            return json_response(payload, 500)
        if kind == "cancelled":
            return Response(status_code=204)
        body = completion_object(
            completion_id=run.completion_id,
            created=run.created,
            model=run.model,
            content="".join(parts),
            reason=payload,
        )
        return json_response(body)


async def _iter_sse(run: TurnRun, first: tuple):
    encoder = ChunkEncoder(
        completion_id=run.completion_id,
        created=run.created,
        model=run.model,
    )
    try:
        encoded = _encode(encoder, first)
        if encoded is not None:
            yield encoded
        if first[0] in ("finish", "error", "cancelled"):
            if first[0] == "finish":
                yield encode_done()
            return
        while True:
            item = await run.pull(_POLL_S)
            if item is None:
                continue
            encoded = _encode(encoder, item)
            if encoded is not None:
                yield encoded
            if item[0] == "finish":
                yield encode_done()
                return
            if item[0] in ("error", "cancelled"):
                return
    finally:
        run.stop_if_open()


def _encode(encoder: ChunkEncoder, item: tuple) -> bytes | None:
    kind, payload = item
    if kind == "content":
        return encode_data(encoder.content(payload))
    if kind == "finish":
        return encode_data(encoder.finish(payload))
    if kind == "error":
        return encode_data(payload)
    return None


async def _pull_until(run: TurnRun, request: Request) -> tuple | None:
    while True:
        item = await run.pull(_POLL_S)
        if item is not None:
            return item
        if await disconnected(request):
            await run.request_stop()
            return None
