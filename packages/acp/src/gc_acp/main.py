"""Точка входа gc-acp: конфиг, затем цикл JSON-RPC на stdin/stdout."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, TextIO

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

from gc_acp.facade import AcpFacade
from gc_acp.protocol import StdoutWriter, stdin_lines


def main(argv: list[str] | None = None) -> int:
    """Синхронный вход консольной команды. Протокол живёт на одном loop."""
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
                program="gc-acp",
                profile_override=args.profile,
            ),
            file=sys.stderr,
        )
        return 1
    rejection = backend_rejection(config)
    if rejection is not None:
        print(rejection, file=sys.stderr)
        return 1
    # stdout с первой строки принадлежит протоколу. console_trace ядро пишет
    # в stdout; безусловный print() (например MCP discovery) — тоже.
    config["console_trace"] = False
    apply_acp_runtime_defaults(config)
    protocol_out = _claim_protocol_stdout()
    try:
        asyncio.run(serve(config, protocol_out))
    finally:
        sys.stdout = protocol_out
    return 0


def _claim_protocol_stdout() -> TextIO:
    """Отдать текущий stdout писателю JSON-RPC, sys.stdout направить в stderr.

    Дальше print() без file= попадает в диагностический канал, а не между
    ответами протокола. Исходный поток возвращаем при выходе из main: процесс
    всё равно завершается, а тесты не должны оставлять подмену.
    """
    protocol_out = sys.stdout
    sys.stdout = sys.stderr
    return protocol_out


def apply_acp_runtime_defaults(config: dict) -> None:
    """Канальные дефолты ACP → runtime-ключи run_agent.

    TUI копирует свои tui_* ключи сам; CLI/server не ставят text_answer_mode
    (→ deny) и держат finish-gate всегда on. ACP — IDE-чат: проза allow,
    finish-gate soft, todo-reminder off, свой special_instructions hint.
    turn_ask_human и turn_intermediate_reply в схему не попадают: их текст
    клиенту не уходит (вопрос — пустой порт, промежуточный ответ — stderr).
    """
    from gc_acp.prompt_assets import acp_special_instructions_path

    mode = config.get("acp_text_answer_mode") or "allow"
    config["text_answer_mode"] = mode

    gate = config.get("acp_finish_gate_mode") or "off"
    config["strict_turn_finish_validation"] = gate != "off"
    config["finish_gate_smart"] = gate == "smart"

    todo_params = dict(config.get("todo_params") or {})
    todo_reminder_steps = config.get("acp_todo_reminder_steps")
    todo_params["reminder_steps"] = (
        0 if todo_reminder_steps is None else int(todo_reminder_steps)
    )
    config["todo_params"] = todo_params

    config["chat_completions_special_instructions_path"] = (
        acp_special_instructions_path()
    )
    _stop_user_invisible_tools(config)


# Текст этих тулов клиент ACP не видит: карточки скрыты, вопрос человека
# возвращает пустую строку, промежуточный ответ печатается в stderr.
_USER_INVISIBLE_TOOLS = (ASK_HUMAN_TOOL_NAME, INTERMEDIATE_REPLY_TOOL_NAME)


def _stop_user_invisible_tools(config: dict) -> None:
    current = config.get("stop_tools")
    names = list(current) if isinstance(current, list) else []
    for name in _USER_INVISIBLE_TOOLS:
        if name not in names:
            names.append(name)
    config["stop_tools"] = names


# backward-compatible alias (tests / call sites)
apply_acp_turn_contract = apply_acp_runtime_defaults


def backend_rejection(config: dict) -> str | None:
    """Harbor и неизвестный os_backend — до цикла протокола."""
    try:
        kind = parse_os_backend_kind(config)
    except ValueError as exc:
        return str(exc)
    if kind == "local":
        return None
    return (
        "gc-acp: os_backend " + kind + " на этом запуске не поддерживается: "
        "cwd сессии — путь на машине редактора"
    )


def build_facade(config: dict, protocol_out: TextIO) -> AcpFacade:
    auth = build_auth_connector(config)
    backend = resolve_os_backend(config)
    cache = ToolsCache(config, os_backend=backend)
    return AcpFacade(
        config=config,
        writer=StdoutWriter(protocol_out),
        agent_factory=_create_agent,
        auth_connector=auth,
        tools_cache=cache,
        os_backend=backend,
    )


async def serve(config: dict, protocol_out: TextIO) -> None:
    facade = build_facade(config, protocol_out)
    try:
        async for line in stdin_lines():
            await facade.handle_line(line)
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
    """Разобрать запуск. Чужие аргументы не роняют процесс.

    Точка входа одна. Свои флаги — только ``--config`` и ``--profile``
    (пустой args у VS Code и ``--profile acp`` в его настройках).
    Остальное клиенты дописывают сами: JetBrains передаёт позиционный
    ``acp``. Такие токены отбрасываются.
    """
    parser = argparse.ArgumentParser(prog="gc-acp")
    parser.add_argument("--config", default=None, help="путь к config.yml")
    parser.add_argument("--profile", default=None, help="имя профиля")
    args, _unknown = parser.parse_known_args(argv)
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
