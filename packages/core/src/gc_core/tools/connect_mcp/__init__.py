from gc_core.tools.connect_mcp.agent_tools import aget_mcp_tools_data_for_config
from gc_core.tools.connect_mcp.api import acall_mcp_tool
from gc_core.tools.connect_mcp.registry import ToolsCache

# Обратная совместимость: старое имя get_mcp_tools_data.
#
# Синхронных форм здесь больше нет. `call_mcp_tool` и
# `get_mcp_tools_data_for_config` были тонкими обёртками над корутинами через
# свой loop; после того как дискавери пошла нативно (шаг A1), их не звал никто.
aget_mcp_tools_data = aget_mcp_tools_data_for_config

__all__ = [
    "ToolsCache",
    "acall_mcp_tool",
    "aget_mcp_tools_data",
    "aget_mcp_tools_data_for_config",
]
