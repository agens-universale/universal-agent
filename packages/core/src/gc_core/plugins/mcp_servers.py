"""Серверная часть снимка набора плагинов (§5.1).

Серверы ``.mcp.json`` плагинов под действующими ключами ``<плагин>_<сервер>``
(или ``<плагин>``), карта «(плагин, исходное имя) → ключ» для перевода
субагентов и диагностика: битый сервер, столкновение ключей, замена записью
пользователя. В ``mcp.servers`` ничего не пишется; значения полей сервера
(там бывают токены) в диагностику и лог не попадают — только имена.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

from pydantic import ValidationError

from gc_core.config_modules.mcp import McpServerConfig
from gc_core.plugins.installed import plugin_data_dir
from gc_core.plugins.manifest import PLUGIN_FORMAT_AP, Plugin

logger = logging.getLogger(__name__)

McpPart = tuple[
    dict[str, McpServerConfig], dict[tuple[str, str], str], tuple[str, ...]
]


def _safe(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "_", text).strip("_") or "x"


def mcp_key(plugin: str, server: str) -> str:
    """``<плагин>_<сервер>``; сервер с именем плагина — просто ``<плагин>``.

    Имя MCP-инструмента ``mcp_<ключ>_<тул>`` ограничено 64 символами,
    удвоенный префикс (``context7_context7``) его съедает.
    """
    plugin, server = _safe(plugin), _safe(server)
    return plugin if server == plugin else f"{plugin}_{server}"


def _data_dir_problem(plugin: Plugin, spec: Mapping[str, Any]) -> str | None:
    """Создать ``${PLUGIN_DATA}`` для stdio-сервера Agent Plugins.

    Создаётся при сборке снимка, а не при чтении плагина (prepare и
    ``/plugins`` ничего не пишут). Недоступный каталог — причина пропуска.
    """
    if plugin.format != PLUGIN_FORMAT_AP or spec.get("transport") != "stdio":
        return None
    try:
        plugin_data_dir(plugin.name)
    except OSError as exc:
        return "каталог данных недоступен " + (
            f"({exc.strerror or type(exc).__name__})"
        )
    return None


def _validated(spec: Mapping[str, Any]) -> tuple[McpServerConfig | None, str]:
    try:
        return McpServerConfig(**spec), ""
    except ValidationError as exc:
        fields = sorted(
            {
                ".".join(map(str, err["loc"])) or "запись"
                for err in exc.errors(include_input=False, include_url=False)
            }
        )
        return None, "неверные поля: " + ", ".join(fields)


def _skipped(plugin: str, name: str, key: str, reason: str) -> str:
    logger.warning(
        "plugin %s mcp server %r (%s) skipped: %s", plugin, name, key, reason
    )
    return f"MCP-сервер {name} плагина {plugin} ({key}) пропущен: {reason}."


def _server(
    plugin: Plugin, name: str, spec: Mapping[str, Any], key: str
) -> tuple[McpServerConfig | None, str | None]:
    """Сервер и ``None`` либо ``None`` и текст диагностики."""
    server, reason = _validated(spec)
    if server is not None:
        reason = _data_dir_problem(plugin, spec) or ""
    if reason:
        return None, _skipped(plugin.name, name, key, reason)
    return server, None


def plugin_mcp_part(
    plugins: tuple[Plugin, ...], user_servers: Mapping[str, Any]
) -> McpPart:
    """Серверы плагинов, ключи и диагностика.

    Ключ, уже занятый другим сервером (другого плагина или того же — после
    санитизации), не перезаписывается: второй пропускается. Одноимённая
    запись пользователя в ``mcp.servers`` заменяет сервер плагина целиком:
    сервер в часть не попадает, ключ в карте остаётся (по нему субагенты
    плагина найдут действующий сервер).
    """
    servers: dict[str, McpServerConfig] = {}
    keys: dict[tuple[str, str], str] = {}
    owners: dict[str, str] = {}
    notices: list[str] = []
    for plugin in plugins:
        for name, spec in plugin.mcp_servers.items():
            key = mcp_key(plugin.name, name)
            if key in owners:
                notices.append(
                    _skipped(
                        plugin.name,
                        name,
                        key,
                        f"ключ уже занят плагином {owners[key]}",
                    )
                )
                continue
            if key in user_servers:
                notices.append(
                    f"MCP-сервер {name} плагина {plugin.name} заменён "
                    f"записью {key} из mcp.servers."
                )
            else:
                server, problem = _server(plugin, name, spec, key)
                if problem is not None:
                    notices.append(problem)
                    continue
                servers[key] = server
            owners[key] = plugin.name
            keys[(plugin.name, name)] = key
    return servers, keys, tuple(notices)
