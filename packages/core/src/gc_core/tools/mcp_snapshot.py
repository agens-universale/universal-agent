"""Снимок MCP «сервер → статус+тулы» из реестра tools_data (технический слой)."""

from typing import Any, Dict

from gc_core.settings import settings_of


def build_mcp_tools_snapshot(
    settings: Any,
    tools_data: Dict[str, Dict[str, Any]],
    mcp_status: Dict[str, Dict[str, Any]] | None = None,
) -> Dict[str, Dict[str, Any]]:
    """server_name → {status, reason, description, tools:[{name,description,tool,title}]}."""
    servers = settings_of(settings).mcp.servers
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
