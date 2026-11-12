"""Срез тулов потребителя сервиса MCP для сборки (спека §5.2, §5.10).

Срез — то, что сборка MCP-тулов получает от источника тулов: записи
реестра, статусы серверов, определения для обёрток и исполнитель вызова.
Форма записей и статусов — та же, что у прежнего обнаружения
(``ToolsCache``), чтобы сборка контекста, экран ``/mcp`` и ACP не замечали
подмены источника.

Здесь только чистые функции над закреплённым срезом потребителя и
определениями вызывающего; состояние и замок — у потребителя.
"""

import logging
from dataclasses import dataclass, replace
from typing import Callable

from gc_core.tools.connect_mcp.client import McpProtocolError
from gc_core.tools.connect_mcp.consumer_status import (
    STATE_FAILED,
    ServerStatus,
)
from gc_core.tools.connect_mcp.failure_reason import classify_failure
from gc_core.tools.connect_mcp.registry import (
    ToolsCache,
    build_checked_entries,
)
from gc_core.tools.connect_mcp.server_identity import connection_fingerprint
from gc_core.tools.connect_mcp.service_records import ServerEntry

logger = logging.getLogger(__name__)


@dataclass
class McpToolsView:
    """Срез тулов для настроек вызывающего.

    ``registry`` — записи ``{полное имя: {"tool", "metadata"}}``;
    ``status`` — ``{ключ: {"status", "reason", "tool_count", ...}}``;
    ``servers_cfg`` — определения серверов для обёрток тулов; ``caller`` —
    исполнитель вызова тула (сигнатура ``acall_mcp_tool``).
    """

    registry: dict[str, dict]
    status: dict[str, dict]
    servers_cfg: dict[str, dict]
    caller: Callable


def server_offer(
    server_key: str, server_cfg: dict, status: ServerStatus
) -> tuple[dict[str, dict], ServerStatus]:
    """Записи реестра тулов, которые сервер предлагает модели, и его
    статус в срезе.

    У ``failed`` тулов нет; у остальных — тулы записи сервиса, оформленные
    по определению вызывающего (whitelist, описание, имена). Список,
    который оформление не принимает (каталог, записанный до проверки
    списка), — отказ этого сервера с кодом ``protocol``: остальные серверы
    среза его не замечают.
    """
    if status.state == STATE_FAILED or not status.tools:
        return {}, status
    try:
        entries = build_checked_entries(
            server_key, server_cfg, list(status.tools), quiet=True
        )
    except McpProtocolError as exc:
        reason = classify_failure(exc, server_cfg)[1]
        # Срез строится на каждой сборке и входе в ход; предупреждение
        # пишет запись сервера при чтении каталога, один раз.
        logger.debug(
            "MCP %s: тулы сервера не предложены: %s", server_key, reason
        )
        failed = replace(
            status,
            state=STATE_FAILED,
            reason_code="protocol",
            reason=reason,
            from_catalog=False,
            tools=(),
        )
        return {}, failed
    return entries, status


def status_record(status: ServerStatus, tool_count: int) -> dict:
    """Запись статуса сервера в форме прежнего обнаружения и с полями
    сервиса; ``tool_count`` — число предложенных тулов (после whitelist)."""
    return {
        "status": status.state,
        "reason": status.reason or None,
        "tool_count": tool_count,
        "reason_code": status.reason_code,
        "retry": status.retry,
        "from_catalog": status.from_catalog,
    }


def callers_pins(
    pinned: dict[str, tuple[ServerEntry, ServerStatus]],
    definitions: dict[str, dict],
) -> dict[str, tuple[ServerEntry, ServerStatus]]:
    """Закреплённые серверы, которые есть в определениях вызывающего с тем
    же отпечатком; остальные в срез не входят."""
    return {
        key: (entry, status)
        for key, (entry, status) in pinned.items()
        if key in definitions
        and entry.fingerprint == connection_fingerprint(definitions[key])
    }


def add_server(
    view: McpToolsView,
    server_key: str,
    server_cfg: dict,
    status: ServerStatus,
    wrapper_cfg: dict,
) -> None:
    """Добавить в срез сервер сервиса: записи, статус, определение."""
    entries, status = server_offer(server_key, server_cfg, status)
    view.registry.update(entries)
    view.status[server_key] = status_record(status, len(entries))
    view.servers_cfg[server_key] = wrapper_cfg


def backend_config(
    cache_config: dict, definitions: dict[str, dict]
) -> dict | None:
    """Конфиг кэша для серверов, исполняемых внутри бэкенда: серверы
    сборки вне ``definitions`` (стороны агента). Таких нет — ``None``."""
    servers = {
        key: cfg
        for key, cfg in cache_config["mcp-servers"].items()
        if key not in definitions
    }
    if not servers:
        return None
    return {**cache_config, "mcp-servers": servers}


async def aadd_backend_part(
    view: McpToolsView, cache: ToolsCache, config: dict
) -> None:
    """Добавить в срез серверы бэкенда — как их сегодня отдаёт кэш."""
    cache.rebind(config)
    view.registry.update(await cache.aget_tools())
    view.status.update(cache.get_status())
    view.servers_cfg.update(config["mcp-servers"])


__all__ = [
    "McpToolsView",
    "aadd_backend_part",
    "add_server",
    "backend_config",
    "callers_pins",
    "server_offer",
    "status_record",
]
