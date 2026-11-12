"""Защита локального API от чужих сайтов в браузере пользователя.

Агент с shell на loopback — классическая цель CSRF и DNS rebinding (Jupyter,
Ollama, MCP Inspector). Три правила, как у них:

- POST — только с ``Content-Type: application/json``. Иначе 415. Такой запрос
  браузер не шлёт без preflight, а preflight чужого origin не пройдёт.
  Исключение — POST без тела с ``Authorization`` (``…/cancel`` из curl):
  этот заголовок браузер тоже не пришлёт со сторонней страницы без preflight.
- CORS выключен, если ``cors_origins`` пуст. ``*`` не бывает: чужой origin
  разрешается только явным списком.
- При loopback-бинде заголовок ``Host`` — только loopback-имя. Чужой домен,
  перерезолвленный в 127.0.0.1, получает 400.
- Тело больше ``max_body_bytes`` — 413 до разбора (и без ``Content-Length``).

Страница чата этого процесса идёт с того же origin и шлёт JSON — правила её не
задевают.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gc_server.chat.errors import ApiError, error_payload

# ponytail: IPv6-loopback (`::1`) не в списке — TrustedHostMiddleware режет
# порт по первому ":". При бинде на `::1` проверка Host выключается.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
_JSON = "application/json"


def install_http_guard(
    app: FastAPI,
    *,
    cors_origins: list[str],
    bind_host: str,
    max_body_bytes: int,
) -> None:
    """Повесить правила на приложение. Последний ``add_middleware`` — внешний."""
    app.add_middleware(_BodyLimit, limit=max_body_bytes)
    app.add_middleware(_JsonPostOnly)
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            allow_private_network=True,
        )
    if bind_host in LOOPBACK_HOSTS:
        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=list(LOOPBACK_HOSTS)
        )


class _JsonPostOnly:
    """POST без ``application/json`` — 415 до маршрута и до разбора тела."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] == "http" and scope["method"] == "POST":
            headers = Headers(scope=scope)
            ctype = headers.get("content-type", "")
            json_post = ctype.split(";", 1)[0].strip().lower() == _JSON
            if not (json_post or _bodyless_authorized(headers)):
                await _reject(
                    415,
                    "Content-Type must be application/json",
                    scope,
                    receive,
                    send,
                )
                return
        await self.app(scope, receive, send)


def _bodyless_authorized(headers: Headers) -> bool:
    """POST без тела и без типа, но с ``Authorization``."""
    return (
        not headers.get("content-type")
        and bool(headers.get("authorization"))
        and headers.get("content-length", "0") == "0"
        and "transfer-encoding" not in headers
    )


async def _reject(status: int, message: str, scope, receive, send) -> None:
    exc = ApiError(status, "invalid_request", message)
    await JSONResponse(error_payload(exc), exc.status)(scope, receive, send)


class _BodyLimit:
    """Тело больше ``limit`` байт — 413: по ``Content-Length`` сразу; тело
    без длины дочитывается до предела (не больше ``limit`` в памяти) и
    отдаётся приложению целиком."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = Headers(scope=scope).get("content-length")
        if length is not None:
            if not length.isdigit() or int(length) > self.limit:
                await _reject(413, _TOO_LARGE, scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        body = await self._read_limited(receive)
        if body is None:
            await _reject(413, _TOO_LARGE, scope, receive, send)
            return
        replay: list[Message] = [
            {"type": "http.request", "body": body, "more_body": False}
        ]

        async def replayed() -> Message:
            return replay.pop() if replay else await receive()

        await self.app(scope, replayed, send)

    async def _read_limited(self, receive: Receive) -> bytes | None:
        """Тело запроса целиком; больше предела — ``None``."""
        chunks: list[bytes] = []
        total, more = 0, True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.limit:
                return None
            chunks.append(chunk)
            more = message.get("more_body", False)
        return b"".join(chunks)


_TOO_LARGE = "request body is too large"
