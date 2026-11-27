"""Снимок MCP «сервер → статус+тулы» из реестра tools_data (технический слой)."""

from typing import Any, Dict

from gc_core.tools.connect_mcp.agent_tools import (
    _assemble_mcp_tools_data,
    mcp_cache_config,
)
from gc_core.tools.connect_mcp.consumer_status import ServerStatus
from gc_core.tools.connect_mcp.consumer_view import (
    server_offer,
    status_record,
)
from gc_core.tools.connect_mcp.effective import effective_mcp_servers


def build_mcp_tools_snapshot(
    settings: Any,
    tools_data: Dict[str, Dict[str, Any]],
    mcp_status: Dict[str, Dict[str, Any]] | None = None,
) -> Dict[str, Dict[str, Any]]:
    """server_name → {status, reason, description, tools}.

    tool: {name, description, tool, title}.
    """
    servers = effective_mcp_servers(settings)
    status_map = mcp_status or {}
    snapshot: Dict[str, Dict[str, Any]] = {}
    for server_name, scfg in servers.items():
        tools = [
            {
                "name": name,
                "description": str(data.get("description", "") or ""),
                "tool": data.get("mcp_tool"),
                "title": data.get("mcp_title"),
            }
            for name, data in tools_data.items()
            if data.get("mcp_server") == server_name
        ]
        st = status_map.get(server_name) or {}
        snapshot[server_name] = {
            "status": st.get("status", "unknown"),
            "reason": st.get("reason"),
            "description": str(scfg.description or ""),
            "tools": tools,
        }
    return snapshot


def overlay_service_statuses(
    snapshot: Dict[str, Dict[str, Any]],
    mcp_status: Dict[str, Dict[str, Any]],
    settings: Any,
    statuses: Dict[str, ServerStatus],
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Снимок и статусы сборки со статусами сервиса (§5.7).

    Серверы ``statuses`` получают статус, причину и тулы сервиса (у
    ``connecting`` и ``cached`` — тулы записи, «из каталога») тем же
    путём, что сборка: записи реестра → ``tools_data`` → снимок. Серверы
    вне сервиса и вне ``settings`` остаются как были. Входные словари не
    меняются.
    """
    servers = mcp_cache_config(settings)["mcp-servers"]
    keys = [key for key in statuses if key in servers]
    registry: Dict[str, Dict[str, Any]] = {}
    fresh_status: Dict[str, Dict[str, Any]] = {}
    for key in keys:
        entries, status = server_offer(key, servers[key], statuses[key])
        registry.update(entries)
        fresh_status[key] = status_record(status, len(entries))
    tools_data, _ = _assemble_mcp_tools_data(
        registry, fresh_status, settings, {key: servers[key] for key in keys}
    )
    fresh = build_mcp_tools_snapshot(settings, tools_data, fresh_status)
    return (
        {**snapshot, **{key: fresh[key] for key in keys}},
        {**mcp_status, **fresh_status},
    )
