"""MCP compatibility exports, loaded only when their worker operation is used."""

__all__ = [
    "ToolsCache",
    "acall_mcp_tool",
    "aget_mcp_tools_data",
    "aget_mcp_tools_data_for_config",
]


def __getattr__(name):
    if name == "ToolsCache":
        from gc_core.tools.connect_mcp.registry import ToolsCache

        return ToolsCache
    if name == "acall_mcp_tool":
        from gc_core.tools.connect_mcp.api import acall_mcp_tool

        return acall_mcp_tool
    if name in {"aget_mcp_tools_data", "aget_mcp_tools_data_for_config"}:
        from gc_core.tools.connect_mcp.agent_tools import (
            aget_mcp_tools_data_for_config,
        )

        return aget_mcp_tools_data_for_config
    raise AttributeError(name)
