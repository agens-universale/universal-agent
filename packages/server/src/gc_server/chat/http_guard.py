"""Защита локального API от чужих сайтов в браузере пользователя.

Агент с shell на loopback — классическая цель CSRF и DNS rebinding (Jupyter,
Ollama, MCP Inspector). Три правила, как у них:

- POST — только с ``Content-Type: application/json``. Иначе 415. Такой запрос
  браузер не шлёт без preflight, а preflight чужого origin не пройдёт.
- CORS выключен, если ``cors_origins`` пуст. ``*`` не бывает: чужой origin
  разрешается только явным списком.
- При loopback-бинде заголовок ``Host`` — только loopback-имя. Чужой домен,
  перерезолвленный в 127.0.0.1, получает 400.

Страница чата этого процесса идёт с того же origin и шлёт JSON — правила её не
задевают.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from gc_server.chat.errors import ApiError, error_payload

# ponytail: IPv6-loopback (`::1`) не в списке — TrustedHostMiddleware режет
# порт по первому ":". При бинде на `::1` проверка Host выключается.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
_JSON = "application/json"


def install_http_guard(
    app: FastAPI, *, cors_origins: list[str], bind_host: str
) -> None:
    """Повесить правила на приложение. Последний ``add_middleware`` — внешний."""
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
            ctype = Headers(scope=scope).get("content-type", "")
            if ctype.split(";", 1)[0].strip().lower() != _JSON:
                exc = ApiError(
                    415,
                    "invalid_request",
                    "Content-Type must be application/json",
                )
                await JSONResponse(error_payload(exc), exc.status)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)
