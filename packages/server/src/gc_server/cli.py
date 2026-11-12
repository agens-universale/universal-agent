"""gc-serve: поднять поверхности сервера (webhook, chat, a2a, swarm).

``gc-serve`` — поверхности из ``server.apis`` config_server.yml;
``gc-serve chat a2a`` — перечисленные. Каждая слушает свой адрес; одинаковый
адрес у нескольких — одно приложение в одном процессе.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from gc_core.config import load_settings, resolve_config_path
from gc_core.config_modules.cli_args import (
    add_setting_flags,
    apply_entry_overrides,
    override_error_text,
    parse_override_args,
    setting_flag_overrides,
    split_setting_paths,
)
from gc_core.config_modules.path_hints import UnknownSettingError
from gc_core.message_policy.mode_messages import headless_mode_warning_text
from gc_core.onboarding import explain_broken_config
from gc_core.security.permission_mode import headless_mode_warning

from gc_server.agent_roles import acheck_roles, aopen_surface_agents
from gc_server.app import AddressConflict, ServerRuntime, aserve, build_surface
from gc_server.config import (
    SURFACES,
    ServerConfig,
    ServerConfigError,
    load_server_config,
)
from gc_server.stores import aopen_process_stores
from gc_server.surface import say

#: Уровни, которые понимает uvicorn; синонимы logging — к ним.
_UVICORN_LOG_LEVELS = frozenset(
    {"critical", "error", "warning", "info", "debug", "trace"}
)
_LOG_LEVEL_ALIASES = {"warn": "warning", "fatal": "critical"}
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
#: Как вернуть edit на поверхностях без человека.
_EDIT_MODE_HINT = (
    "в config_server.yml — agent_overrides: "
    "security.permissions.default_mode: edit"
)
_HEADLESS = ("webhook", "a2a")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = _parse_args(argv)
    try:
        cfg = load_server_config(args.server_config)
    except ServerConfigError as exc:
        say(str(exc))
        return 1
    names = list(args.surfaces or cfg.server.apis)
    if len(set(names)) != len(names):
        say("поверхность названа дважды: " + " ".join(names))
        return 2
    try:
        loaded = load_settings(args.config, profile_override=args.profile)
    except ValueError as exc:
        print(
            explain_broken_config(
                resolve_config_path(args.config),
                exc,
                program="gc-serve",
                profile_override=args.profile,
            ),
            file=sys.stderr,
        )
        return 1
    try:
        loaded = apply_entry_overrides(loaded, args.overrides)
    except ValueError as exc:
        say(override_error_text(exc))
        return 2
    log_level = normalize_log_level(cfg.server.log_level)
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    say(
        "config_server.yml: "
        + (cfg.config_path or "не найден, взяты умолчания")
    )
    return asyncio.run(_amain(loaded, cfg, names, log_level))


async def _amain(loaded, cfg: ServerConfig, names, log_level: str) -> int:
    from gc_stores.configured import StorageConfigError

    try:
        stores = await aopen_process_stores(loaded)
    except StorageConfigError as exc:
        say(str(exc))
        return 1
    runtime = ServerRuntime(base=loaded, config=cfg, stores=stores)
    try:
        runtime.surface_agents = await aopen_surface_agents(loaded, cfg, names)
        await acheck_roles(runtime, names)
        return await _aserve_surfaces(runtime, names, log_level)
    except (StorageConfigError, ServerConfigError) as exc:
        say(str(exc))
        return 1
    finally:
        for store in runtime.surface_agents.values():
            await store.close()
        if stores is not None:
            await stores.aclose()


async def _aserve_surfaces(runtime: ServerRuntime, names, log_level) -> int:
    try:
        surfaces = [build_surface(runtime, name) for name in names]
    except ServerConfigError as exc:
        say(str(exc))
        return 1
    _warn(runtime.config, surfaces)
    try:
        await aserve(runtime, surfaces, log_level=log_level)
    except AddressConflict as exc:
        say(str(exc))
        return 1
    return 0


def _warn(cfg: ServerConfig, surfaces) -> None:
    """Тревоги по состоянию: открытая наружу поверхность без токена и режим,
    который без человека не работает. Старт не прерывается."""
    for surface in surfaces:
        section = cfg.surface(surface.name)
        if not section.auth_token:
            host = str(section.host)
            where = "наружу" if host not in _LOOPBACK else "локально"
            say(
                f"{surface.name}: без авторизации (auth_token пуст), "
                f"слушает {where}: {host}:{section.port}"
            )
        settings = getattr(surface, "settings", None)
        if surface.name in _HEADLESS and settings is not None:
            mode = headless_mode_warning(settings)
            if mode is not None:
                say(
                    f"{surface.name}: "
                    + headless_mode_warning_text(mode, _EDIT_MODE_HINT)
                )


def normalize_log_level(raw) -> str:
    """``server.log_level`` → уровень uvicorn; неизвестный — ``info`` с
    предупреждением: поднятый сервер лучше упавшего."""
    level = str(raw if raw is not None else "info").strip().lower()
    level = _LOG_LEVEL_ALIASES.get(level, level)
    if level not in _UVICORN_LOG_LEVELS:
        say(
            f"server.log_level={raw!r} uvicorn не понимает (допустимы: "
            f"{', '.join(sorted(_UVICORN_LOG_LEVELS))}) — беру info"
        )
        return "info"
    return level


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Поверхности — первыми аргументами (``gc-serve chat a2a --config …``):
    значения флагов настроек (``--agent.max_steps 5``) иначе читались бы
    поверхностями."""
    rest = list(sys.argv[1:] if argv is None else argv)
    surfaces: list[str] = []
    while rest and not rest[0].startswith("-"):
        surfaces.append(rest.pop(0))
    parser = argparse.ArgumentParser(
        prog="gc-serve",
        allow_abbrev=False,
        usage="gc-serve [webhook|chat|a2a|swarm ...] [опции]",
    )
    parser.add_argument("--config", default=None, help="путь к config.yml")
    parser.add_argument("--profile", default=None, help="имя профиля")
    parser.add_argument(
        "--server-config", default=None, help="путь к config_server.yml"
    )
    add_setting_flags(parser)
    unknown_surfaces = [name for name in surfaces if name not in SURFACES]
    if unknown_surfaces:
        parser.error(
            f"неизвестная поверхность {unknown_surfaces[0]!r}: "
            f"допустимы {', '.join(SURFACES)}"
        )
    args, unknown = parser.parse_known_args(rest)
    args.surfaces = surfaces
    args.overrides, foreign = split_setting_paths(
        parse_override_args(list(unknown))
    )
    if foreign:
        # Прежнее плоское имя (`--max_steps`) — с подсказкой пути.
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


if __name__ == "__main__":
    raise SystemExit(main())
