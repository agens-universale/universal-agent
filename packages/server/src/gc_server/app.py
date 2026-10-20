"""Процесс сервера: поверхности по адресам, один event loop, общий старт и стоп.

Поверхности с одинаковым ``host:port`` живут в одном приложении: ASGI-диспетчер
отдаёт запрос той поверхности, чей маршрут совпал. На каждый адрес — свой
uvicorn; сокеты открываются до старта поверхностей, поэтому занятый адрес —
отказ до того, как что-то запустилось. Сигнал остановки гасит все серверы
вместе; идущие ходы поверхности снимают сами (``astop``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
from dataclasses import dataclass, field
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI
from gc_core.aio.agent import AsyncAgent
from gc_core.config import cli_forces_legacy_model
from gc_core.settings import LoadedSettings
from gc_core.threads.facade import Threads
from starlette.responses import JSONResponse
from starlette.routing import Match

from gc_server.config import ServerConfig, ServerConfigError, surface_settings
from gc_server.surface import Surface, http_origin, routes_of, say

logger = logging.getLogger(__name__)

#: Сколько uvicorn ждёт открытые соединения после сигнала остановки.
GRACEFUL_SHUTDOWN_SECONDS = 5


class AddressConflict(ServerConfigError):
    """Адрес поверхностей занят или не может быть общим."""


@dataclass
class ServerRuntime:
    """Общее состояние процесса: настройки агента, конфиг сервера, хранилища."""

    base: LoadedSettings
    config: ServerConfig
    #: Связка хранилищ из ``storage:``; ``None`` — файловые из настроек.
    stores: Any = None
    _create_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def surface_settings(self, name: str) -> LoadedSettings:
        return surface_settings(self.base, self.config, name)

    def store_kwargs(self) -> dict[str, Any]:
        """Хранилища для ``acreate_run_context`` и ``AsyncAgent.create`` —
        только нефайловые части ``storage:``; файловые строит сам прогон."""
        from gc_core.stores import run_store_kwargs

        return run_store_kwargs(self.base, self.stores)

    def threads(self) -> Threads:
        store = self.store_kwargs().get("thread_store")
        if store is not None:
            return Threads(store)
        return Threads.from_settings(self.base)

    def response_index(self) -> Any:
        """Индекс ответов chat в хранилище процесса; ``None`` — файловый по
        умолчанию фасада (с файлами develop для отката)."""
        if "thread_store" not in self.store_kwargs():
            return None
        from gc_server.chat.responses.index_store import ResponseIndex

        return ResponseIndex(self.threads())

    def agent_factory(self, settings: LoadedSettings) -> Callable[..., Any]:
        """Фабрика агентов фасадов chat и a2a: создание — по одному (MCP-
        дискавери не размножается), хранилища — процесса."""
        force_legacy = cli_forces_legacy_model(settings)

        async def factory(
            agent_settings: LoadedSettings,
            *,
            auth_connector: Any,
            tools_cache: Any,
            os_backend: Any,
        ) -> AsyncAgent:
            async with self._create_lock:
                return await AsyncAgent.create(
                    agent_settings,
                    auth_connector=auth_connector,
                    tools_cache=tools_cache,
                    os_backend=os_backend,
                    force_legacy=force_legacy,
                    **self.store_kwargs(),
                )

        return factory


def build_surface(runtime: ServerRuntime, name: str) -> Surface:
    """Поверхность по имени; модули грузятся только нужные."""
    if name == "webhook":
        from gc_server.webhook.surface import WebhookSurface

        return WebhookSurface(runtime)
    if name == "swarm":
        from gc_server.swarm.surface import SwarmSurface

        return SwarmSurface(runtime)
    if name == "chat":
        from gc_server.chat.surface import ChatSurface

        return ChatSurface(runtime)
    if name == "a2a":
        from gc_server.a2a.surface import A2aSurface

        return A2aSurface(runtime)
    raise ServerConfigError(f"неизвестная поверхность {name!r}")


def group_surfaces(
    cfg: ServerConfig, names: list[str]
) -> dict[tuple[str, int], list[str]]:
    """Поверхности по адресам. webhook и swarm на одном адресе — ошибка: у
    них одинаковые ручки."""
    groups: dict[tuple[str, int], list[str]] = {}
    for name in names:
        groups.setdefault(cfg.address(name), []).append(name)
    for (host, port), members in groups.items():
        if {"webhook", "swarm"} <= set(members):
            raise AddressConflict(
                f"webhook и swarm на одном адресе {host}:{port} — у них "
                "одинаковые ручки; разведите порты (webhook.port, swarm.port)"
            )
    return groups


class Dispatcher:
    """ASGI-приложение адреса: запрос — поверхности, чей маршрут совпал
    (полностью; иначе — по пути при другом методе), без совпадений — 404."""

    def __init__(self, apps: list[FastAPI]) -> None:
        self._apps = apps

    async def __call__(self, scope, receive, send) -> None:
        app = self._pick(scope) if scope["type"] != "lifespan" else None
        if app is None:
            if scope["type"] == "http":
                await JSONResponse({"detail": "Not Found"}, status_code=404)(
                    scope, receive, send
                )
            return
        await app(scope, receive, send)

    def _pick(self, scope) -> FastAPI | None:
        partial = None
        for app in self._apps:
            for route in app.router.routes:
                match, _ = route.matches(scope)
                if match is Match.FULL:
                    return app
                if match is Match.PARTIAL and partial is None:
                    partial = app
        return partial


def bind_socket(host: str, port: int, names: list[str]) -> socket.socket:
    """Сокет адреса — до старта чего-либо: занятый адрес даёт понятный отказ
    вместо трейсбека uvicorn посреди запуска."""
    try:
        info = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )[0]
        sock = socket.socket(info[0], info[1], info[2])
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(info[4])
    except OSError as exc:
        raise AddressConflict(
            f"адрес {host}:{port} ({', '.join(names)}) недоступен: "
            f"{exc.strerror or exc}. Порт занят другим процессом — запустите "
            "эти поверхности одним процессом gc-serve или разведите порты"
        ) from None
    sock.set_inheritable(True)
    return sock


class _Server(uvicorn.Server):
    """uvicorn без своих обработчиков сигналов: несколько серверов в одном
    процессе гасятся вместе, общим обработчиком."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


def _asgi(surfaces: list[Surface]) -> Any:
    apps = [s.app() for s in surfaces]
    return apps[0] if len(apps) == 1 else Dispatcher(apps)


def _announce(app: Any, host: str, port: int) -> None:
    apps = app._apps if isinstance(app, Dispatcher) else [app]
    origin = http_origin(host, port)
    for one in apps:
        for method, path in routes_of(one):
            if path.startswith(("/docs", "/redoc", "/openapi")):
                continue
            say(f"{method} {origin}{path}")


def _install_stop(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (RuntimeError, ValueError):
            # Windows и не главный поток: остановка — флагом снаружи.
            continue


async def aserve(
    runtime: ServerRuntime,
    surfaces: list[Surface],
    *,
    log_level: str = "info",
    stop: asyncio.Event | None = None,
) -> None:
    """Поднять поверхности и ждать сигнала (или ``stop``)."""
    by_name = {s.name: s for s in surfaces}
    groups = group_surfaces(runtime.config, list(by_name))
    sockets: dict[tuple[str, int], socket.socket] = {}
    try:
        for (host, port), names in groups.items():
            sockets[(host, port)] = bind_socket(host, port, names)
        stop = stop or asyncio.Event()
        _install_stop(stop)
        for surface in surfaces:
            await surface.astart()
        servers = []
        for (host, port), names in groups.items():
            app = _asgi([by_name[n] for n in names])
            _announce(app, host, port)
            server = _Server(
                uvicorn.Config(
                    app,
                    lifespan="off",
                    log_level=log_level,
                    access_log=True,
                    timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
                )
            )
            servers.append((server, sockets[(host, port)]))
        await _run_servers(servers, surfaces, stop)
    finally:
        for surface in reversed(surfaces):
            await _safe_stop(surface)
        for sock in sockets.values():
            sock.close()


async def _run_servers(servers, surfaces, stop: asyncio.Event) -> None:
    tasks = [
        asyncio.create_task(server.serve(sockets=[sock]))
        for server, sock in servers
    ]

    async def watch() -> None:
        stopper = asyncio.create_task(stop.wait())
        await asyncio.wait(
            [stopper, *tasks], return_when=asyncio.FIRST_COMPLETED
        )
        stopper.cancel()
        for server, _sock in servers:
            server.should_exit = True
        # Соединения держат блокирующие запросы (ход a2a, ожидание ответа
        # chat): поверхности снимают ходы сразу, не дожидаясь закрытия.
        for surface in surfaces:
            await _safe_stop(surface)

    watcher = asyncio.create_task(watch())
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        await watcher


async def _safe_stop(surface: Surface) -> None:
    try:
        await surface.astop()
    except Exception:
        logger.exception("остановка поверхности %s", surface.name)


__all__ = [
    "GRACEFUL_SHUTDOWN_SECONDS",
    "AddressConflict",
    "Dispatcher",
    "ServerRuntime",
    "aserve",
    "bind_socket",
    "build_surface",
    "group_surfaces",
]
