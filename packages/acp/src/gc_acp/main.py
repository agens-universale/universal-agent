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
    cli_forces_legacy_model,
    load_settings,
    resolve_config_path,
)
from gc_core.config_modules.auth import build_auth_connector
from gc_core.config_modules.cli_args import (
    add_setting_flags,
    apply_entry_overrides,
    foreign_flags_notice,
    override_error_text,
    parse_override_args,
    setting_flag_overrides,
    split_setting_paths,
)
from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
)
from gc_core.onboarding import explain_broken_config
from gc_core.settings import LoadedSettings
from gc_core.tools.connect_mcp.registry import ToolsCache

from gc_acp.facade import AcpFacade
from gc_acp.protocol import StdoutWriter, stdin_lines


def main(argv: list[str] | None = None) -> int:
    """Синхронный вход консольной команды. Протокол живёт на одном loop."""
    _force_utf8_stdio()
    args = _parse_args(argv)
    if args.abbreviated:
        print(
            "[gc-acp] флаги не сокращаются: " + ", ".join(args.abbreviated),
            file=sys.stderr,
        )
        return 2
    try:
        loaded = load_settings(args.config, profile_override=args.profile)
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
    # Как у gc-cli: --путь.настройки value поверх профиля и config.yml. В
    # stdout не печатаем — канал занят JSON-RPC.
    for line in foreign_flags_notice("gc-acp", args.foreign_flags):
        print(line, file=sys.stderr)
    try:
        loaded = apply_entry_overrides(loaded, args.overrides)
    except ValueError as exc:
        print(f"[gc-acp] {override_error_text(exc)}", file=sys.stderr)
        return 2
    rejection = backend_rejection(loaded)
    if rejection is not None:
        print(rejection, file=sys.stderr)
        return 1
    # stdout с первой строки принадлежит протоколу. console_trace ядро пишет
    # в stdout; безусловный print() (например MCP discovery) — тоже.
    loaded = apply_acp_runtime_defaults(
        loaded.with_overrides(
            {"logging.console_trace": False}, layer="runtime"
        )
    )
    protocol_out = _claim_protocol_stdout()
    try:
        asyncio.run(serve(loaded, protocol_out))
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


def apply_acp_runtime_defaults(loaded: LoadedSettings) -> LoadedSettings:
    """Канальные дефолты ACP (секция ``acp``) → настройки и рантайм хода.

    TUI копирует свои tui_* ключи сам; CLI/server не ставят text_answer_mode
    (→ deny) и держат finish-gate всегда on. ACP — IDE-чат: проза allow,
    finish-gate soft, todo-reminder off, свой special_instructions hint.
    turn_ask_human и turn_intermediate_reply в схему не попадают: их текст
    клиенту не уходит (вопрос — пустой порт, промежуточный ответ — stderr).
    """
    from gc_acp.prompt_assets import acp_special_instructions_path

    acp = loaded.settings.acp
    gate = acp.finish_gate_mode
    return loaded.with_overrides(
        {
            "agent.strict_turn_finish_validation": gate != "off",
            "todo.reminder_steps": acp.todo_reminder_steps,
            "paths.chat_completions_special_instructions_path": (
                acp_special_instructions_path()
            ),
            "agent.stop_tools": _stop_user_invisible_tools(
                loaded.settings.agent.stop_tools
            ),
        },
        layer="runtime",
    ).with_runtime(
        text_answer_mode=acp.text_answer_mode,
        finish_gate_smart=gate == "smart",
    )


# Текст этих тулов клиент ACP не видит: карточки скрыты, вопрос человека
# возвращает пустую строку, промежуточный ответ печатается в stderr.
_USER_INVISIBLE_TOOLS = (ASK_HUMAN_TOOL_NAME, INTERMEDIATE_REPLY_TOOL_NAME)


def _stop_user_invisible_tools(current: Any) -> list[str]:
    names = list(current or [])
    for name in _USER_INVISIBLE_TOOLS:
        if name not in names:
            names.append(name)
    return names


# backward-compatible alias (tests / call sites)
apply_acp_turn_contract = apply_acp_runtime_defaults


def backend_rejection(loaded: Any) -> str | None:
    """Harbor и неизвестный os_backend — до цикла протокола."""
    try:
        kind = parse_os_backend_kind(loaded)
    except ValueError as exc:
        return str(exc)
    if kind == "local":
        return None
    return (
        "gc-acp: os_backend " + kind + " на этом запуске не поддерживается: "
        "cwd сессии — путь на машине редактора"
    )


def build_facade(loaded: LoadedSettings, protocol_out: TextIO) -> AcpFacade:
    auth = build_auth_connector(loaded)
    backend = resolve_os_backend(loaded)
    cache = ToolsCache(os_backend=backend)
    return AcpFacade(
        settings=loaded,
        writer=StdoutWriter(protocol_out),
        agent_factory=_create_agent,
        auth_connector=auth,
        tools_cache=cache,
        os_backend=backend,
    )


async def serve(loaded: LoadedSettings, protocol_out: TextIO) -> None:
    facade = build_facade(loaded, protocol_out)
    try:
        async for line in stdin_lines():
            await facade.handle_line(line)
    finally:
        await facade.shutdown()


async def _create_agent(
    loaded: LoadedSettings,
    *,
    auth_connector: Any,
    tools_cache: Any,
    os_backend: Any,
) -> AsyncAgent:
    return await AsyncAgent.create(
        loaded,
        auth_connector=auth_connector,
        tools_cache=tools_cache,
        os_backend=os_backend,
        force_legacy=cli_forces_legacy_model(loaded),
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Разобрать запуск. Чужие аргументы не роняют процесс.

    Свои флаги — ``--config``, ``--profile`` (пустой args у VS Code и
    ``--profile acp`` в его настройках) и флаги-настройки gc-cli
    (``--model``, ``--completions_url``, ``--level``). ``--путь.настройки``
    — правка конфига, как у gc-cli; незнакомый путь — ошибка. Остальное
    клиенты дописывают сами (JetBrains — позиционный ``acp``,
    ``--experimental``): такие токены пропускаются (``foreign_flags``).
    """
    parser = argparse.ArgumentParser(prog="gc-acp", allow_abbrev=False)
    parser.add_argument("--config", default=None, help="путь к config.yml")
    parser.add_argument("--profile", default=None, help="имя профиля")
    add_setting_flags(parser)
    args, unknown = parser.parse_known_args(argv)
    args.overrides, args.foreign_flags = split_setting_paths(
        parse_override_args(list(unknown))
    )
    args.overrides.update(setting_flag_overrides(args))
    # Сокращение ``--config``/``--profile`` (``--conf``, ``--prof``) — не флаг
    # клиента: пропуск запустил бы агента на конфиге по умолчанию. Только эти
    # два и только от четырёх символов: короткий флаг клиента (``--c``,
    # ``--mod``) — его собственный, а не наше сокращение.
    args.abbreviated = sorted(
        {
            f"--{name} (--{match})"
            for name in args.foreign_flags
            for match in _CONFIG_FLAGS
            if len(name) >= 4 and name != match and match.startswith(name)
        }
    )
    return args


#: Свои флаги, сокращение которых меняет конфиг запуска.
_CONFIG_FLAGS = ("config", "profile")


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", newline="\n")
        except (OSError, ValueError):
            continue
