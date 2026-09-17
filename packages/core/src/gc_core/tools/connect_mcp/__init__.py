from gc_core.tools.connect_mcp.agent_tools import get_mcp_tools_data_for_config
from gc_core.tools.connect_mcp.api import call_mcp_tool
from gc_core.tools.connect_mcp.registry import get_all_tools, ToolsCache

# Обратная совместимость: старое имя get_mcp_tools_data
get_mcp_tools_data = get_mcp_tools_data_for_config

__all__ = [
    "call_mcp_tool",
    "get_all_tools",
    "ToolsCache",
    "get_mcp_tools_data",
    "get_mcp_tools_data_for_config",
]
