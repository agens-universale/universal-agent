"""Точка входа gc-chat: конфиг, канальные дефолты, оба HTTP-фасада."""

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
from gc_core.config import load_settings, resolve_config_path
from gc_core.config_modules.auth import build_auth_connector
from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
)
from gc_core.onboarding import explain_broken_config
from gc_core.settings import LoadedSettings
from gc_core.tools.connect_mcp.registry import ToolsCache

from gc_chat.app import create_app
from gc_chat.completions.facade import CompletionsFacade
from gc_chat.model_route import reserved_model_reason
from gc_chat.responses.facade import ResponsesFacade


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
                program="gc-chat",
                profile_override=args.profile,
            ),
            file=sys.stderr,
        )
        return 1
    rejection = backend_rejection(loaded)
    if rejection is not None:
        print(rejection, file=sys.stderr)
        return 1
    collision = reserved_model_reason(str(loaded.settings.agent.model or ""))
    if collision is not None:
        print(collision, file=sys.stderr)
        return 1
    loaded = apply_chat_runtime_defaults(loaded)
    if not loaded.settings.chat_api.auth_token:
        print("gc-chat: сервер без авторизации", file=sys.stderr)
    asyncio.run(serve(loaded))
    return 0


def apply_chat_runtime_defaults(loaded: LoadedSettings) -> LoadedSettings:
    """Общие дефолты процесса (секция ``chat_api``) → новые настройки и рантайм.

    ``agent.strict_turn_finish_validation`` и чужой ``text_answer_mode``
    этот процесс перекрывает. Напоминания ``session_todo`` ставятся
    из ``chat_api.todo_reminder_steps`` (дефолт 0). Тул не снимается.
    ``turn_ask_human`` и ``turn_intermediate_reply`` снимаются.
    Ролевая вставка сюда не пишется: у каждого фасада свой файл,
    путь ставится на копию настроек перед созданием агента.
    """
    chat_api = loaded.settings.chat_api
    gate = chat_api.finish_gate_mode
    return loaded.with_overrides(
        {
            "agent.strict_turn_finish_validation": gate != "off",
            "todo.reminder_steps": chat_api.todo_reminder_steps,
            "agent.stop_tools": _stop_channel_tools(
                loaded.settings.agent.stop_tools
            ),
        },
        layer="runtime",
    ).with_runtime(
        text_answer_mode=chat_api.text_answer_mode,
        finish_gate_smart=gate == "smart",
    )


# Вопрос человеку и промежуточная реплика в схему не входят. Канал их
# не показывает: вопрос уходит прозой, прогресс тоже.
_CHANNEL_HIDDEN_TOOLS = (ASK_HUMAN_TOOL_NAME, INTERMEDIATE_REPLY_TOOL_NAME)


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
        "gc-chat: os_backend " + kind + " на этом запуске не поддерживается: "
        "рабочий каталог хода — путь на машине этого процесса"
    )


def build_facades(
    loaded: LoadedSettings, agent_factory: Any
) -> tuple[ResponsesFacade, CompletionsFacade]:
    auth = build_auth_connector(loaded)
    backend = resolve_os_backend(loaded)
    cache = ToolsCache(os_backend=backend)
    shared = {
        "settings": loaded,
        "agent_factory": agent_factory,
        "auth_connector": auth,
        "tools_cache": cache,
        "os_backend": backend,
    }
    return ResponsesFacade(**shared), CompletionsFacade(**shared)


async def serve(loaded: LoadedSettings) -> None:
    lock = asyncio.Lock()

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
            )

    responses, completions = build_facades(loaded, factory)
    app = create_app(responses, completions)
    host = loaded.settings.chat_api.host
    port = loaded.settings.chat_api.port
    _log_routes(app, host, port)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="info",
            access_log=True,
        )
    )
    try:
        await server.serve()
    finally:
        await responses.shutdown()


def _log_routes(app: Any, host: str, port: int) -> None:
    """Метод и адрес каждой ручки. В stderr, до bind."""
    origin = _origin(host, port)
    for method, path in _routes(app):
        print(f"gc-chat: {method} {origin}{path}", file=sys.stderr)


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
    parser = argparse.ArgumentParser(prog="gc-chat")
    parser.add_argument("--config", default=None, help="путь к config.yml")
    parser.add_argument("--profile", default=None, help="имя профиля")
    return parser.parse_args(argv)


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", newline="\n")
        except (OSError, ValueError):
            continue
