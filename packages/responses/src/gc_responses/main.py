"""Точка входа gc-responses: конфиг, канальные дефолты, HTTP."""

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
    build_auth_connector,
    load_config_ex,
    resolve_config_path,
)
from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
)
from gc_core.onboarding import explain_broken_config
from gc_core.tools.connect_mcp.registry import ToolsCache

from gc_responses.facade import ResponsesFacade
from gc_responses.http_api import create_app
from gc_responses.prompt_assets import responses_special_instructions_path


def main(argv: list[str] | None = None) -> int:
    """Синхронный вход. Сокет открывается только после конфига и проверки бэкенда."""
    _force_utf8_stdio()
    args = _parse_args(argv)
    try:
        config, _meta = load_config_ex(
            args.config, profile_override=args.profile
        )
    except ValueError as exc:
        print(
            explain_broken_config(
                resolve_config_path(args.config),
                exc,
                program="gc-responses",
                profile_override=args.profile,
            ),
            file=sys.stderr,
        )
        return 1
    rejection = backend_rejection(config)
    if rejection is not None:
        print(rejection, file=sys.stderr)
        return 1
    apply_responses_runtime_defaults(config)
    if not str(config.get("responses_auth_token") or ""):
        print("gc-responses: сервер без авторизации", file=sys.stderr)
    asyncio.run(serve(config))
    return 0


def apply_responses_runtime_defaults(config: dict) -> None:
    """Канальные дефолты Responses → runtime-ключи.

    ``agent.strict_turn_finish_validation`` и чужой ``text_answer_mode``
    этот процесс перекрывает. Напоминания ``session_todo`` ставятся
    из ``responses.todo_reminder_steps`` (дефолт 0). Тул не снимается.
    ``turn_ask_human`` и ``turn_intermediate_reply`` снимаются,
    ролевая вставка режима подменяет
    ``chat_completions_special_instructions_path``.
    """
    mode = config.get("responses_text_answer_mode") or "allow"
    config["text_answer_mode"] = mode
    gate = config.get("responses_finish_gate_mode") or "off"
    config["strict_turn_finish_validation"] = gate != "off"
    config["finish_gate_smart"] = gate == "smart"
    _apply_todo_reminder(config)
    _disable_channel_tools(config)
    config["chat_completions_special_instructions_path"] = (
        responses_special_instructions_path()
    )


def _apply_todo_reminder(config: dict) -> None:
    """Напоминания чеклиста. 0 — не напоминать. Сам тул остаётся."""
    todo_reminder_steps = config.get("responses_todo_reminder_steps")
    if todo_reminder_steps is None:
        todo_reminder_steps = 0
    todo_params = dict(config.get("todo_params") or {})
    todo_params["reminder_steps"] = int(todo_reminder_steps)
    config["todo_params"] = todo_params


def _disable_channel_tools(config: dict) -> None:
    """Вопрос человеку и промежуточная реплика в схему не входят.

    Канал их не показывает: вопрос уходит прозой, прогресс тоже.
    """
    raw = config.get("stop_tools")
    names = list(raw) if isinstance(raw, list) else []
    for tool_name in (ASK_HUMAN_TOOL_NAME, INTERMEDIATE_REPLY_TOOL_NAME):
        if tool_name not in names:
            names.append(tool_name)
    config["stop_tools"] = names


def backend_rejection(config: dict) -> str | None:
    """Harbor и неизвестный os_backend — до bind."""
    try:
        kind = parse_os_backend_kind(config)
    except ValueError as exc:
        return str(exc)
    if kind == "local":
        return None
    return (
        "gc-responses: os_backend "
        + kind
        + " на этом запуске не поддерживается: "
        "рабочий каталог хода — путь на машине этого процесса"
    )


def build_facade(config: dict) -> ResponsesFacade:
    auth = build_auth_connector(config)
    backend = resolve_os_backend(config)
    cache = ToolsCache(config, os_backend=backend)
    return ResponsesFacade(
        config=config,
        agent_factory=_create_agent,
        auth_connector=auth,
        tools_cache=cache,
        os_backend=backend,
    )


async def serve(config: dict) -> None:
    facade = build_facade(config)
    app = create_app(facade)
    host = str(config.get("responses_host") or "127.0.0.1")
    port = int(config.get("responses_port") or 8090)
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
        await facade.shutdown()


async def _create_agent(
    config: dict,
    *,
    auth_connector: Any,
    tools_cache: Any,
    os_backend: Any,
) -> AsyncAgent:
    return await AsyncAgent.create(
        config,
        auth_connector=auth_connector,
        tools_cache=tools_cache,
        os_backend=os_backend,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gc-responses")
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
