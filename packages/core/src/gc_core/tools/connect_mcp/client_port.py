"""The MCP operation port: what an MCP call is, independent of where it runs.

`AgentRuntimeClient` speaks all six transports from the agent's own process —
this is the behaviour gc-cli has always had, moved behind an interface so a
second locus can exist beside it without either knowing about the other.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import requests

from gc_core.tools.connect_mcp.client import (
    _MCP_SESSIONS,
    _call_mcp_tool,
    _get_or_init_session_id,
    _tools_list_request,
)
from gc_core.tools.connect_mcp.persistent_stdio import (
    PersistentStdioRegistry,
    is_persistent_stdio,
)
from gc_core.tools.connect_mcp.stream_client import (
    call_tool_stream_http,
    call_tool_stream_sse,
    call_tool_stream_stdio,
    list_tools_stream_http,
    list_tools_stream_sse,
    list_tools_stream_stdio,
)

_SUPPORTED_TRANSPORTS = (
    "http",
    "sse",
    "stdio",
    "stream_sse",
    "stream_http",
    "stream_stdio",
)


class McpClientPort(ABC):
    """One MCP server, seen as three operations."""

    def __init__(self, server_name: str, server_cfg: Dict[str, Any]) -> None:
        self.server_name = server_name
        self.server_cfg = server_cfg if isinstance(server_cfg, dict) else {}

    @abstractmethod
    def list_tools(self, *, timeout: float) -> List[Dict[str, Any]]:
        """Raw `tools/list` entries, exactly as the server returned them."""

    @abstractmethod
    def call_tool(
        self, tool_name: str, arguments: Dict[str, Any], *, timeout: float
    ) -> Any:
        """Raw `tools/call` response, exactly as the transport produced it.

        Not coerced to a dict: legacy transports may answer with a list or a
        string, and `call_mcp_tool` has a branch that hands such a value to the
        model as-is.
        """


def _effective_transport(server_cfg: Dict[str, Any]) -> str:
    """`persistent_session: true` routes legacy sse/http through the SDK client.

    Same rule as before, kept in one place instead of duplicated in registry
    and api.
    """
    transport = str(server_cfg.get("transport") or "")
    if server_cfg.get("persistent_session"):
        if transport == "sse":
            return "stream_sse"
        if transport == "http":
            return "stream_http"
    return transport


class AgentRuntimeClient(McpClientPort):
    """All six transports, spoken from the agent's own process."""

    def _require_transport(self) -> str:
        transport = str(self.server_cfg.get("transport") or "")
        if transport not in _SUPPORTED_TRANSPORTS:
            raise ValueError(f"unsupported transport: {transport!r}")
        return transport

    def _url(self) -> str:
        return str(self.server_cfg.get("url") or "")

    def _token(self) -> Optional[str]:
        token = self.server_cfg.get("token")
        return str(token) if token else None

    def list_tools(self, *, timeout: float) -> List[Dict[str, Any]]:
        transport = self._require_transport()
        effective = _effective_transport(self.server_cfg)
        url = self._url()
        token = self._token()

        if effective == "stream_sse":
            return list_tools_stream_sse(
                url=url, server_cfg=self.server_cfg, token=token, timeout=timeout
            )
        if effective == "stream_http":
            return list_tools_stream_http(
                url=url, server_cfg=self.server_cfg, token=token, timeout=timeout
            )
        if transport == "stream_stdio":
            return list_tools_stream_stdio(
                server_cfg=self.server_cfg, timeout=timeout
            )
        if is_persistent_stdio(self.server_cfg):
            session = PersistentStdioRegistry.instance().get_or_create(
                self.server_name, self.server_cfg
            )
            resp = session.call("tools/list", {}, timeout)
            result = resp.get("result") if isinstance(resp, dict) else None
            tools = result.get("tools", []) if isinstance(result, dict) else []
            return tools if isinstance(tools, list) else []

        session_id = _get_or_init_session_id(
            self.server_name, url, self.server_cfg, token=token
        )
        if not session_id:
            raise RuntimeError(
                f"Failed to initialize MCP session for server '{self.server_name}'"
            )
        try:
            return _tools_list_request(
                url, session_id=session_id, server_cfg=self.server_cfg, token=token
            )
        except requests.HTTPError:
            _MCP_SESSIONS.pop(self.server_name, None)
            session_id = _get_or_init_session_id(
                self.server_name, url, self.server_cfg, token=token
            )
            if not session_id:
                raise RuntimeError(
                    f"Failed to re-initialize MCP session for server "
                    f"'{self.server_name}'"
                )
            return _tools_list_request(
                url, session_id=session_id, server_cfg=self.server_cfg, token=token
            )

    def call_tool(
        self, tool_name: str, arguments: Dict[str, Any], *, timeout: float
    ) -> Any:
        transport = self._require_transport()
        effective = _effective_transport(self.server_cfg)
        url = self._url()
        token = self._token()

        if effective == "stream_sse":
            return call_tool_stream_sse(
                url=url,
                server_cfg=self.server_cfg,
                token=token,
                timeout=timeout,
                tool_name=tool_name,
                arguments=arguments,
            )
        if effective == "stream_http":
            return call_tool_stream_http(
                url=url,
                server_cfg=self.server_cfg,
                token=token,
                timeout=timeout,
                tool_name=tool_name,
                arguments=arguments,
            )
        if transport == "stream_stdio":
            return call_tool_stream_stdio(
                server_cfg=self.server_cfg,
                timeout=timeout,
                tool_name=tool_name,
                arguments=arguments,
            )
        if is_persistent_stdio(self.server_cfg):
            session = PersistentStdioRegistry.instance().get_or_create(
                self.server_name, self.server_cfg
            )
            return session.call(
                "tools/call", {"name": tool_name, "arguments": arguments}, timeout
            )

        session_id = _get_or_init_session_id(
            self.server_name, url, self.server_cfg, token=token, timeout=timeout
        )
        if not session_id:
            raise RuntimeError(
                f"Failed to initialize MCP session for server '{self.server_name}'"
            )

        def do_call(sid: str) -> Any:
            return _call_mcp_tool(
                url=url,
                session_id=sid,
                tool_name=tool_name,
                arguments=arguments,
                server_cfg=self.server_cfg,
                token=token,
                timeout=timeout,
                server_key=self.server_name,
            )

        try:
            data = do_call(session_id)
        except requests.HTTPError:
            _MCP_SESSIONS.pop(self.server_name, None)
            session_id = _get_or_init_session_id(
                self.server_name, url, self.server_cfg, token=token, timeout=timeout
            )
            if not session_id:
                raise RuntimeError(
                    f"Failed to re-initialize MCP session for server "
                    f"'{self.server_name}'"
                )
            data = do_call(session_id)

        if data == {} and transport == "sse":
            _MCP_SESSIONS.pop(self.server_name, None)
            session_id = _get_or_init_session_id(
                self.server_name, url, self.server_cfg, token=token, timeout=timeout
            )
            if session_id:
                data = do_call(session_id)
        return data


__all__ = ["McpClientPort", "AgentRuntimeClient"]
