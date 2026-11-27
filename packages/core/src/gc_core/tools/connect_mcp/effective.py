"""Действующие MCP-серверы: серверы конфига и серверы плагинов (§5.1).

Серверы плагинов приходят готовыми из снимка набора в ``RuntimeEnv``; модуль
плагинов слой MCP не импортирует.
"""

from typing import Any

from gc_core.config_modules.mcp import McpServerConfig
from gc_core.settings import runtime_of, settings_of


def effective_mcp_servers(settings: Any) -> dict[str, McpServerConfig]:
    """``mcp.servers``, затем серверы плагинов; одноимённая запись
    пользователя заменяет сервер плагина целиком."""
    own = settings_of(settings).mcp.servers
    plugin = runtime_of(settings).plugins.mcp_servers
    return {
        **own,
        **{key: srv for key, srv in plugin.items() if key not in own},
    }
