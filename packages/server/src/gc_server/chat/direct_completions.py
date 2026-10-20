"""Один ``chat/completions`` в бэкенд. Тело и SSE не переписываются."""

from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from gc_core.settings import LoadedSettings, settings_of
from gc_core.utils.async_http import get_async_client
from gc_core.utils.connect_backend import (
    acreate_call_headers,
    resolve_network_params,
)
from gc_core.utils.http_proxy import resolve_proxies

from gc_server.chat.completions.errors import ApiError, error_payload

logger = logging.getLogger(__name__)

_JSON = "application/json"
_SSE = "text/event-stream"


class DirectCompletions:
    """Прямой запрос: без агента, без треда, один вызов бэкенда."""

    def __init__(
        self, *, settings: LoadedSettings, auth_connector: Any = None
    ) -> None:
        self._settings = settings
        self._auth = auth_connector

    async def forward(self, body: dict, request: Request) -> Response:
        if not isinstance(body.get("messages"), list):
            return _invalid("messages is required")
        opened = await self._open(body)
        if isinstance(opened, Response):
            return opened
        return await _answer(opened, body, request)

    async def _open(self, body: dict) -> Any:
        try:
            url, verify, cert, timeout, proxies = _network(self._settings)
            headers = await _backend_headers(self._settings, self._auth)
            client = get_async_client(
                verify=verify, cert=cert, proxies=proxies
            )
            upstream = client.build_request(
                "POST",
                url,
                json=body,
                headers=headers,
                timeout=timeout,
            )
            return await client.send(upstream, stream=True)
        except Exception:
            logger.exception("direct chat/completions")
            return _server_error()


def _network(settings: LoadedSettings) -> tuple:
    url = settings_of(settings).backend.completions_url
    if not isinstance(url, str) or url == "":
        raise RuntimeError("completions url is empty")
    verify, cert, _retry, timeout, proxy, _embedded = resolve_network_params(
        settings, None
    )
    proxies = resolve_proxies(proxy, os.environ, url)
    return url, verify, cert, timeout, proxies


async def _backend_headers(
    settings: LoadedSettings, auth_connector: Any
) -> dict:
    """Те же заголовки, что у хода агента к своей модели. Bearer клиента нет."""
    state = SimpleNamespace(state_id=str(uuid4()), x_target=None)
    ctx = SimpleNamespace(settings=settings, auth_connector=auth_connector)
    return await acreate_call_headers(state, ctx, resolved=None)


async def _answer(upstream: Any, body: dict, request: Request) -> Response:
    try:
        if upstream.status_code >= 400:
            return await _backend_failure(upstream)
        if body.get("stream") is True:
            return StreamingResponse(
                _relay(upstream, request), media_type=_SSE
            )
        raw = await upstream.aread()
    except Exception:
        await _close(upstream)
        logger.exception("direct chat/completions")
        return _server_error()
    await _close(upstream)
    return Response(
        content=raw,
        status_code=upstream.status_code,
        media_type=_JSON,
    )


async def _backend_failure(upstream: Any) -> Response:
    try:
        raw = await upstream.aread()
    except Exception:
        raw = b""
    await _close(upstream)
    if _json_object(raw):
        return Response(
            content=raw,
            status_code=upstream.status_code,
            media_type=_JSON,
        )
    err = ApiError(upstream.status_code, "server_error", "backend error")
    return _json(error_payload(err), upstream.status_code)


async def _relay(upstream: Any, request: Request):
    try:
        async for chunk in upstream.aiter_bytes():
            if await _disconnected(request):
                return
            yield chunk
    finally:
        await _close(upstream)


def _json_object(raw: bytes) -> bool:
    try:
        payload = json.loads(raw)
    except ValueError:
        return False
    return isinstance(payload, dict)


def _invalid(message: str) -> Response:
    err = ApiError(400, "invalid_request", message)
    return _json(error_payload(err), 400)


def _server_error() -> Response:
    err = ApiError(500, "server_error", "backend request failed")
    return _json(error_payload(err), 500)


def _json(payload: dict, status_code: int) -> Response:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Response(content=raw, status_code=status_code, media_type=_JSON)


async def _close(upstream: Any) -> None:
    close = getattr(upstream, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        logger.exception("direct chat/completions close")


async def _disconnected(request: Any) -> bool:
    probe = getattr(request, "is_disconnected", None)
    if probe is None:
        return False
    try:
        return bool(await probe())
    except Exception:
        return False
