"""Маршруты ``/v1/models`` и ``/v1/chat/completions``."""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from gc_server.chat.completions.errors import ApiError, error_payload
from gc_server.chat.completions.facade import CompletionsFacade
from gc_server.chat.completions.sse import (
    ChunkEncoder,
    completion_object,
    encode_data,
    encode_done,
)
from gc_server.chat.completions.turn import TurnRun
from gc_server.chat.direct_completions import DirectCompletions
from gc_server.chat.http_guard import install_http_guard
from gc_server.chat.model_route import completions_route, models_body

_JSON = "application/json"
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
    denied = _unauthorized(request, facade.auth_token)
    if denied is not None:
        return denied
    return _json(models_body(facade.model_id))


async def create_completion(request: Request) -> Response:
    facade = _facade(request)
    denied = _unauthorized(request, facade.auth_token)
    if denied is not None:
        return denied
    body, rejected = await _object_body(request)
    if rejected is not None:
        return rejected
    route = completions_route(body.get("model"), facade.model_id)
    if route is None:
        exc = ApiError(400, "invalid_request", "unknown model")
        return _json(error_payload(exc), 400)
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
        return _json(error_payload(exc), exc.status)
    except Exception as exc:
        err = ApiError(500, "server_error", str(exc) or "server error")
        return _json(error_payload(err), 500)


async def _stream_response(run: TurnRun, request: Request) -> Response:
    first = await _pull_until(run, request)
    if first is None:
        return Response(status_code=204)
    kind, payload = first
    if kind == "error":
        return _json(payload, 500)
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
            return _json(payload, 500)
        if kind == "cancelled":
            return Response(status_code=204)
        body = completion_object(
            completion_id=run.completion_id,
            created=run.created,
            model=run.model,
            content="".join(parts),
            reason=payload,
        )
        return _json(body)


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
        if await _disconnected(request):
            await run.request_stop()
            return None


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


async def _disconnected(request: Any) -> bool:
    probe = getattr(request, "is_disconnected", None)
    if probe is None:
        return False
    try:
        return bool(await probe())
    except Exception:
        return False
