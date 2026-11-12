"""Общее у маршрутов Responses, Chat Completions и диалогов: токен, тело, JSON."""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from gc_server.chat.errors import ApiError, error_payload

_JSON = "application/json"
_NOT_OBJECT = "request body must be an object"


def unauthorized(request: Request, token: str) -> Response | None:
    """Ответ 401, если токен задан, а ``Authorization: Bearer`` не тот."""
    if token == "":
        return None
    header = request.headers.get("authorization")
    if _bearer_ok(header, token):
        return None
    exc = ApiError(401, "invalid_api_key", "invalid api key")
    return json_response(error_payload(exc), 401)


def _bearer_ok(header: str | None, token: str) -> bool:
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return False
    got = header[len("Bearer ") :]
    if len(got) != len(token):
        return False
    return secrets.compare_digest(got, token)


async def object_body(request: Request) -> tuple[Any, Response | None]:
    """Тело-объект JSON или готовый ответ 400."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        exc = ApiError(400, "invalid_request", _NOT_OBJECT)
        return None, json_response(error_payload(exc), 400)
    return body, None


async def authorized_body(
    request: Request, token: str
) -> tuple[Any, Response | None]:
    """Токен, затем тело-объект: первое, что не прошло, — готовым ответом."""
    denied = unauthorized(request, token)
    if denied is not None:
        return None, denied
    return await object_body(request)


def json_response(payload: dict, status_code: int = 200) -> Response:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Response(content=raw, status_code=status_code, media_type=_JSON)


async def disconnected(request: Any) -> bool:
    """Клиент отключился (у запроса без такой проверки — нет)."""
    probe = getattr(request, "is_disconnected", None)
    if probe is None:
        return False
    try:
        return bool(await probe())
    except Exception:
        return False
