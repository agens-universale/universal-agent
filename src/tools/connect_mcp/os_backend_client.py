"""One MCP operation = one stateless exec inside the tool-layer environment."""
from typing import Any, Dict, List

from tools.connect_mcp.client_port import McpClientPort


class OsBackendStatelessClient(McpClientPort):
    def __init__(
        self, server_name: str, server_cfg: Dict[str, Any], *, os_backend: Any
    ) -> None:
        super().__init__(server_name, server_cfg)
        self.os_backend = os_backend

    def list_tools(self, *, timeout: float) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def call_tool(
        self, tool_name: str, arguments: Dict[str, Any], *, timeout: float
    ) -> Dict[str, Any]:
        raise NotImplementedError


__all__ = ["OsBackendStatelessClient"]
