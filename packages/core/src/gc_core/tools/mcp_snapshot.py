"""Снимок MCP «сервер → статус+тулы» из реестра tools_data (технический слой)."""
from typing import Any, Dict, List


def build_mcp_tools_snapshot(
    config_dict: Dict[str, Any],
    tools_data: Dict[str, Dict[str, Any]],
    mcp_status: Dict[str, Dict[str, Any]] | None = None,
) -> Dict[str, Dict[str, Any]]:
    """server_name → {status, reason, description, tools:[{name,description}]}."""
    servers = config_dict.get("mcp-servers") or {}
    status_map = mcp_status or {}
    snapshot: Dict[str, Dict[str, Any]] = {}
    for server_name, scfg in servers.items():
        prefix = f"mcp_{server_name}_"
        tools = [
            {"name": name, "description": str(data.get("description", "") or "")}
            for name, data in tools_data.items()
            if name.startswith(prefix)
        ]
        st = status_map.get(server_name) or {}
        snapshot[server_name] = {
            "status": st.get("status", "unknown"),
            "reason": st.get("reason"),
            "description": str((scfg or {}).get("description") or ""),
            "tools": tools,
        }
    return snapshot
