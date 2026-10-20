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
from gc_core.thread_store import sanitize_system_id

from gc_server.config import RESERVED_SYSTEMS


class Surface(Protocol):
    name: str

    def app(self) -> FastAPI: ...

    async def astart(self) -> None: ...

    async def astop(self) -> None: ...


def install_bearer(
    app: FastAPI, token: str | None, *, public_paths: tuple[str, ...] = ()
) -> None:
    """Пустой токен — без проверки; иначе ``Authorization: Bearer`` или 403.
    ``public_paths`` — без токена (карточка агента A2A)."""
    if not token:
        return
    expected = str(token)

    @app.middleware("http")
    async def _bearer(request: Request, call_next):
        if request.url.path in public_paths:
            return await call_next(request)
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


def allowed_system(system: str) -> str:
    """``system`` в той форме, в какой его хранит хранилище
    (``sanitize_system_id``), если он не чужой; иначе — 400.

    Проверять сырую строку нельзя: хранилище нормализует её само, и «cli!» или
    «.cli» стали бы тредом «cli» в обход проверки."""
    try:
        normalized = sanitize_system_id(system)
    except ValueError as exc:
        raise http_error(400, str(exc)) from None
    if normalized in RESERVED_SYSTEMS:
        raise http_error(
            400,
            f"system {normalized!r} занят другой поверхностью сервера или "
            f"интерактивными сессиями (занятые: "
            f"{', '.join(sorted(RESERVED_SYSTEMS))})",
        )
    return normalized


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
