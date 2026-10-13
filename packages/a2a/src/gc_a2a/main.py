"""Точка входа gc-a2a: конфиг, канальные дефолты, HTTP-фасад."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import uvicorn
from gc_core.aio.agent import AsyncAgent
from gc_core.backends.factory import (
    parse_os_backend_kind,
    resolve_os_backend,
)
from gc_core.config import (
    cli_forces_legacy_model,
    load_settings,
    resolve_config_path,
)
from gc_core.config_modules.auth import build_auth_connector
from gc_core.config_modules.cli_args import (
    add_setting_flags,
    apply_entry_overrides,
    override_error_text,
    parse_override_args,
    setting_flag_overrides,
    split_setting_paths,
)
from gc_core.config_modules.path_hints import UnknownSettingError
from gc_core.constants.tool_names import INTERMEDIATE_REPLY_TOOL_NAME
from gc_core.onboarding import explain_broken_config
from gc_core.settings import LoadedSettings
from gc_core.tools.connect_mcp.registry import ToolsCache

from gc_a2a.facade import A2aFacade
from gc_a2a.http_api import create_app


def main(argv: list[str] | None = None) -> int:
    """Синхронный вход. Сокет открывается только после конфига и проверки бэкенда."""
    _force_utf8_stdio()
    args = _parse_args(argv)
    try:
        loaded = load_settings(args.config, profile_override=args.profile)
    except ValueError as exc:
        print(
            explain_broken_config(
                resolve_config_path(args.config),
                exc,
                program="gc-a2a",
                profile_override=args.profile,
            ),
            file=sys.stderr,
        )
        return 1
    try:
        loaded = apply_entry_overrides(loaded, args.overrides)
    except ValueError as exc:
        print(f"[gc-a2a] {override_error_text(exc)}", file=sys.stderr)
        return 2
    rejection = backend_rejection(loaded)
    if rejection is not None:
        print(rejection, file=sys.stderr)
        return 1
    loaded = apply_a2a_runtime_defaults(loaded)
    rejection = auth_rejection(loaded)
    if rejection is not None:
        print(rejection, file=sys.stderr)
        return 1
    if not loaded.settings.a2a.auth_token:
        print("gc-a2a: сервер без авторизации", file=sys.stderr)
    asyncio.run(serve(loaded))
    return 0


def apply_a2a_runtime_defaults(loaded: LoadedSettings) -> LoadedSettings:
    """Дефолты процесса (секция ``a2a``) → настройки и рантайм хода.

    ``turn_ask_human`` остаётся в схеме: его текст — вопрос задачи.
    ``turn_intermediate_reply`` снимается: прогресс пиру не отдаётся.
    """
    section = loaded.settings.a2a
    gate = section.finish_gate_mode
    return loaded.with_overrides(
        {
            "agent.strict_turn_finish_validation": gate != "off",
            "todo.reminder_steps": section.todo_reminder_steps,
            "agent.stop_tools": _stop_channel_tools(
                loaded.settings.agent.stop_tools
            ),
        },
        layer="runtime",
    ).with_runtime(
        text_answer_mode=section.text_answer_mode,
        finish_gate_smart=gate == "smart",
    )


_CHANNEL_HIDDEN_TOOLS = (INTERMEDIATE_REPLY_TOOL_NAME,)


def _stop_channel_tools(current: Any) -> list[str]:
    """Полный список ``agent.stop_tools``: правка заменяет, а не сливает."""
    names = list(current or [])
    for tool_name in _CHANNEL_HIDDEN_TOOLS:
        if tool_name not in names:
            names.append(tool_name)
    return names


def backend_rejection(loaded: Any) -> str | None:
    """Harbor и неизвестный os_backend — до bind."""
    try:
        kind = parse_os_backend_kind(loaded)
    except ValueError as exc:
        return str(exc)
    if kind == "local":
        return None
    return (
        "gc-a2a: os_backend " + kind + " на этом запуске не поддерживается: "
        "рабочий каталог хода — путь на машине этого процесса"
    )


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def auth_rejection(loaded: Any) -> str | None:
    """Не-loopback без токена — до bind: агент с тулами нельзя светить в сеть."""
    section = loaded.settings.a2a
    if section.auth_token:
        return None
    host = section.host.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host in _LOOPBACK_HOSTS:
        return None
    return (
        "gc-a2a: a2a.host "
        + section.host
        + " не loopback — нужен непустой a2a.auth_token"
    )


def build_facade(loaded: LoadedSettings, agent_factory: Any) -> A2aFacade:
    auth = build_auth_connector(loaded)
    backend = resolve_os_backend(loaded)
    cache = ToolsCache(os_backend=backend)
    return A2aFacade(
        settings=loaded,
        agent_factory=agent_factory,
        auth_connector=auth,
        tools_cache=cache,
        os_backend=backend,
    )


async def serve(loaded: LoadedSettings) -> None:
    lock = asyncio.Lock()
    force_legacy = cli_forces_legacy_model(loaded)

    async def factory(
        agent_settings: LoadedSettings,
        *,
        auth_connector: Any,
        tools_cache: Any,
        os_backend: Any,
    ) -> AsyncAgent:
        async with lock:
            return await AsyncAgent.create(
                agent_settings,
                auth_connector=auth_connector,
                tools_cache=tools_cache,
                os_backend=os_backend,
                force_legacy=force_legacy,
            )

    facade = build_facade(loaded, factory)
    app = create_app(facade)
    host = loaded.settings.a2a.host
    port = loaded.settings.a2a.port
    _log_routes(app, host, port)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host=host, port=port, log_level="info", access_log=True
        )
    )
    try:
        await server.serve()
    finally:
        await facade.shutdown()


def _log_routes(app: Any, host: str, port: int) -> None:
    origin = _origin(host, port)
    for method, path in _routes(app):
        print(f"gc-a2a: {method} {origin}{path}", file=sys.stderr)


def _routes(app: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for route in getattr(app, "routes", ()):
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not isinstance(path, str):
            continue
        for method in sorted(methods):
            if method == "HEAD":
                continue
            found.append((method, path))
    return found


def _origin(host: str, port: int) -> str:
    shown = host
    if ":" in host and not host.startswith("["):
        shown = "[" + host + "]"
    return "http://" + shown + ":" + str(port)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gc-a2a", allow_abbrev=False)
    parser.add_argument("--config", default=None, help="путь к config.yml")
    parser.add_argument("--profile", default=None, help="имя профиля")
    add_setting_flags(parser)
    args, unknown = parser.parse_known_args(argv)
    args.overrides, foreign = split_setting_paths(
        parse_override_args(list(unknown))
    )
    if foreign:
        parser.error(str(UnknownSettingError(foreign[0], prefix="--")))
    args.overrides.update(setting_flag_overrides(args))
    return args


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", newline="\n")
        except (OSError, ValueError):
            continue
