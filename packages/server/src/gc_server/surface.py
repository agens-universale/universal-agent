"""Поверхность сервера: что у всех общего — протокол, токен, разбор запроса.

Поверхность (webhook, swarm, chat, a2a) отдаёт своё FastAPI-приложение и
умеет стартовать и останавливаться; процесс (``gc_server.app``) собирает
поверхности по адресам и запускает uvicorn.
"""

from __future__ import annotations

import secrets
import sys
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from gc_server.config import RESERVED_SYSTEMS


class Surface(Protocol):
    name: str

    def app(self) -> FastAPI: ...

    async def astart(self) -> None: ...

    async def astop(self) -> None: ...


def install_bearer(app: FastAPI, token: str | None) -> None:
    """Пустой токен — без проверки; иначе ``Authorization: Bearer`` или 403."""
    if not token:
        return
    expected = str(token)

    @app.middleware("http")
    async def _bearer(request: Request, call_next):
        scheme, _, got = request.headers.get("Authorization", "").partition(
            " "
        )
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            got.encode(), expected.encode()
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "status": "forbidden",
                    "detail": "invalid or missing token",
                },
            )
        return await call_next(request)


def http_error(status: int, payload: str, **extra: Any) -> HTTPException:
    """Ошибка в общей форме ответа вебхука и swarm."""
    return HTTPException(
        status_code=status,
        detail={"status": "error", "payload": payload, **extra},
    )


def check_system_allowed(system: str) -> None:
    """``system`` другой поверхности — 400: вебхук и swarm не пишут в её
    треды."""
    if system in RESERVED_SYSTEMS:
        raise http_error(
            400,
            f"system {system!r} занят другой поверхностью сервера "
            f"(занятые: {', '.join(sorted(RESERVED_SYSTEMS))})",
        )


async def read_json_object(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise http_error(400, "invalid JSON body") from None
    if not isinstance(payload, dict):
        raise http_error(400, "JSON body must be an object")
    return payload


def routes_of(app: Any) -> list[tuple[str, str]]:
    """Метод и путь каждой ручки приложения (без HEAD)."""
    found: list[tuple[str, str]] = []
    for route in getattr(app, "routes", ()):
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not isinstance(path, str):
            continue
        found.extend((m, path) for m in sorted(methods) if m != "HEAD")
    return found


def http_origin(host: str, port: int) -> str:
    shown = host
    if ":" in host and not host.startswith("["):
        shown = "[" + host + "]"
    return f"http://{shown}:{port}"


def say(text: str) -> None:
    """Строка оператору в stderr (до и после настройки логирования)."""
    print(f"gc-serve: {text}", file=sys.stderr)
