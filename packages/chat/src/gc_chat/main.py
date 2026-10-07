"""Точка входа gc-chat: конфиг, канальные дефолты, оба HTTP-фасада."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import uvicorn
import yaml
from gc_core.aio.agent import AsyncAgent
from gc_core.backends.factory import (
    parse_os_backend_kind,
    resolve_os_backend,
)
from gc_core.config import (
    apply_cli_overrides,
    build_auth_connector,
    cli_forces_legacy_model,
    load_config_ex,
    resolve_config_path,
)
from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
)
from gc_core.onboarding import explain_broken_config
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
        config, _meta = load_config_ex(
            args.config, profile_override=args.profile
        )
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
    # Как у gc-cli / gc-acp: --ключ value поверх профиля/config.yml.
    apply_cli_overrides(config, extra_overrides=args.overrides)
    if cli_forces_legacy_model(config):
        config.setdefault("compat_notices", []).append(
            "--model: уровни отключены на этот запуск (легаси-форс)"
        )
    rejection = backend_rejection(config)
    if rejection is not None:
        print(rejection, file=sys.stderr)
        return 1
    collision = reserved_model_reason(str(config.get("model") or ""))
    if collision is not None:
        print(collision, file=sys.stderr)
        return 1
    apply_chat_runtime_defaults(config)
    if not str(config.get("chat_api_auth_token") or ""):
        print("gc-chat: сервер без авторизации", file=sys.stderr)
    asyncio.run(serve(config))
    return 0


def apply_chat_runtime_defaults(config: dict) -> None:
    """Общие дефолты процесса → runtime-ключи.

    ``agent.strict_turn_finish_validation`` и чужой ``text_answer_mode``
    этот процесс перекрывает. Напоминания ``session_todo`` ставятся
    из ``chat_api.todo_reminder_steps`` (дефолт 0). Тул не снимается.
    ``turn_ask_human`` и ``turn_intermediate_reply`` снимаются.
    Ролевая вставка сюда не пишется: у каждого фасада свой файл,
    путь ставится на копию конфига перед созданием агента.
    """
    mode = config.get("chat_api_text_answer_mode") or "allow"
    config["text_answer_mode"] = mode
    gate = config.get("chat_api_finish_gate_mode") or "off"
    config["strict_turn_finish_validation"] = gate != "off"
    config["finish_gate_smart"] = gate == "smart"
    _apply_todo_reminder(config)
    _disable_channel_tools(config)


def _apply_todo_reminder(config: dict) -> None:
    """Напоминания чеклиста. 0 — не напоминать. Сам тул остаётся."""
    todo_reminder_steps = config.get("chat_api_todo_reminder_steps")
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
        "gc-chat: os_backend " + kind + " на этом запуске не поддерживается: "
        "рабочий каталог хода — путь на машине этого процесса"
    )


def build_facades(
    config: dict, agent_factory: Any
) -> tuple[ResponsesFacade, CompletionsFacade]:
    auth = build_auth_connector(config)
    backend = resolve_os_backend(config)
    cache = ToolsCache(config, os_backend=backend)
    shared = {
        "config": config,
        "agent_factory": agent_factory,
        "auth_connector": auth,
        "tools_cache": cache,
        "os_backend": backend,
    }
    return ResponsesFacade(**shared), CompletionsFacade(**shared)


async def serve(config: dict) -> None:
    lock = asyncio.Lock()
    force_legacy = cli_forces_legacy_model(config)

    async def factory(
        agent_config: dict,
        *,
        auth_connector: Any,
        tools_cache: Any,
        os_backend: Any,
    ) -> AsyncAgent:
        async with lock:
            return await AsyncAgent.create(
                agent_config,
                auth_connector=auth_connector,
                tools_cache=tools_cache,
                os_backend=os_backend,
                force_legacy=force_legacy,
            )

    responses, completions = build_facades(config, factory)
    app = create_app(responses, completions)
    host = str(config.get("chat_api_host") or "127.0.0.1")
    port = int(config.get("chat_api_port") or 8090)
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


def _coerce_cli_value(raw: str) -> Any:
    """Строка CLI → тип через yaml.safe_load (как у gc-cli)."""
    try:
        value = yaml.safe_load(raw)
    except Exception:
        return raw
    return raw if value is None else value


def _parse_unknown_args(tokens: list[str]) -> dict[str, Any]:
    """``--param value`` / ``--param=value`` / ``--flag`` → оверрайды конфига.

    Позиционные токены пропускаются. Контракт тот же, что у ``gc-cli`` /
    ``gc-acp``: любой ``--ключ`` ложится в runtime-конфиг поверх профиля и
    файловых слоёв.
    """
    overrides: dict[str, Any] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("--"):
            i += 1
            continue
        key_part = token[2:]
        value: Any = True
        if "=" in key_part:
            key, raw_val = key_part.split("=", 1)
            value = _coerce_cli_value(raw_val)
        else:
            key = key_part
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                value = _coerce_cli_value(tokens[i + 1])
                i += 1
        if key:
            overrides[key] = value
        i += 1
    return overrides


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Разобрать запуск. Чужие аргументы не роняют процесс.

    Свои флаги — ``--config`` и ``--profile``. Остальные ``--ключ`` —
    оверрайды конфига (как у gc-cli / gc-acp). Позиционные токены
    отбрасываются.
    """
    parser = argparse.ArgumentParser(prog="gc-chat")
    parser.add_argument("--config", default=None, help="путь к config.yml")
    parser.add_argument("--profile", default=None, help="имя профиля")
    args, unknown = parser.parse_known_args(argv)
    args.overrides = _parse_unknown_args(list(unknown))
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
