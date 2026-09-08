"""The MCP operation port: what an MCP call is, independent of where it runs.

`AgentRuntimeClient` speaks all six transports from the agent's own process —
this is the behaviour gc-cli has always had, moved behind an interface so a
second locus can exist beside it without either knowing about the other.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import httpx

from gc_core.tools.connect_mcp.client import (
    _MCP_SESSIONS,
    forget_session,
    _acall_mcp_tool,
    _aget_or_init_session_id,
    _atools_list_request,
    _call_mcp_tool,
    _get_or_init_session_id,
    _tools_list_request,
)
from gc_core.tools.connect_mcp.persistent_stdio import (
    PersistentStdioRegistry,
    is_persistent_stdio,
)
from gc_core.tools.connect_mcp.abandonable import run_abandonable
from gc_core.tools.connect_mcp.stream_client import (
    acall_tool_stream_http,
    acall_tool_stream_sse,
    acall_tool_stream_stdio,
    alist_tools_stream_http,
    alist_tools_stream_sse,
    alist_tools_stream_stdio,
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

    @abstractmethod
    async def alist_tools(self, *, timeout: float) -> List[Dict[str, Any]]:
        """Асинхронный двойник ``list_tools``.

        Порт приехал с develop синхронным, а дискавери и вызовы тулов здесь
        нативно асинхронны. Без этой пары async-путь звал бы синхронный метод и
        морозил loop на всё время сетевого обмена (у stdio — ещё и подпроцесса),
        то есть терял ровно то свойство, ради которого ветка и делалась.

        Двойник, а не замена: синхронный вход остаётся — на нём сидят прямые
        вызовы и тесты.
        """

    @abstractmethod
    async def acall_tool(
        self, tool_name: str, arguments: Dict[str, Any], *, timeout: float
    ) -> Any:
        """Асинхронный двойник ``call_tool`` — см. ``alist_tools``."""


def _effective_transport(server_cfg: Dict[str, Any]) -> str:
    """Транспорт, которым реально пойдёт запрос, — по ОБЩИМ правилам.

    Здесь была третья копия правил выбора, и она умела только половину:
    поднимала легаси-транспорт до SDK лишь при ``persistent_session``. Про
    депрекейт ``sse`` она не знала, поэтому непersistent ``transport: sse``
    уходил в одноразовый ``_arequest_sse`` — тот самый путь, про который
    докстрока политики говорит «405 на spec-серверах». Дискавери при этом шла
    через ``stream_sse``, и в лог писалось «Запрос отправлен через stream_sse»:
    сообщение было правдой про дискавери и ложью про вызов тула.

    Единственный источник правил — ``transport_policy.resolve_effective_transport``.
    """
    from gc_core.tools.connect_mcp.transport_policy import resolve_effective_transport

    return resolve_effective_transport(server_cfg)


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
        except httpx.HTTPStatusError:
            forget_session(self.server_name)
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
        except httpx.HTTPStatusError:
            forget_session(self.server_name)
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
            forget_session(self.server_name)
            session_id = _get_or_init_session_id(
                self.server_name, url, self.server_cfg, token=token, timeout=timeout
            )
            if session_id:
                data = do_call(session_id)
        return data

    # --- асинхронные двойники -------------------------------------------------
    #
    # SDK-транспорты (stream_*) асинхронны насквозь — идём в них нативно.
    # Персистентный stdio и легаси-HTTP асинхронной формы не имеют: их уводим в
    # `run_abandonable` (поток-демон, который переживёт отмену по таймауту —
    # прервать блокирующий сокет всё равно нечем). Тот же приём, которым
    # дискавери уже пользовалась до появления порта.

    async def alist_tools(self, *, timeout: float) -> List[Dict[str, Any]]:
        transport = self._require_transport()
        effective = _effective_transport(self.server_cfg)
        url = self._url()
        token = self._token()

        if effective == "stream_sse":
            return await alist_tools_stream_sse(
                url=url, server_cfg=self.server_cfg, token=token, timeout=timeout
            )
        if effective == "stream_http":
            return await alist_tools_stream_http(
                url=url, server_cfg=self.server_cfg, token=token, timeout=timeout
            )
        if transport == "stream_stdio":
            return await alist_tools_stream_stdio(
                server_cfg=self.server_cfg, timeout=timeout
            )
        if is_persistent_stdio(self.server_cfg):
            # Долгоживущая сессия сидит на `subprocess.Popen` СОЗНАТЕЛЬНО:
            # asyncio-подпроцесс привязан к создавшему его loop, а эту сессию
            # заводит дискавери, зовут тулы и закрывает `arun` — три разных
            # loop. Прервать блокирующий обмен нечем, поэтому поток-демон.
            return await run_abandonable(self.list_tools, timeout=timeout)

        session_id = await _aget_or_init_session_id(
            self.server_name, url, self.server_cfg, token=token
        )
        if not session_id:
            raise RuntimeError(
                f"Failed to initialize MCP session for server '{self.server_name}'"
            )
        try:
            return await _atools_list_request(
                url, session_id=session_id, server_cfg=self.server_cfg, token=token
            )
        except httpx.HTTPStatusError:
            forget_session(self.server_name)
            session_id = await _aget_or_init_session_id(
                self.server_name, url, self.server_cfg, token=token
            )
            if not session_id:
                raise RuntimeError(
                    f"Failed to re-initialize MCP session for server "
                    f"'{self.server_name}'"
                )
            return await _atools_list_request(
                url, session_id=session_id, server_cfg=self.server_cfg, token=token
            )

    async def acall_tool(
        self, tool_name: str, arguments: Dict[str, Any], *, timeout: float
    ) -> Any:
        transport = self._require_transport()
        effective = _effective_transport(self.server_cfg)
        url = self._url()
        token = self._token()

        if effective == "stream_sse":
            return await acall_tool_stream_sse(
                url=url,
                server_cfg=self.server_cfg,
                token=token,
                timeout=timeout,
                tool_name=tool_name,
                arguments=arguments,
            )
        if effective == "stream_http":
            return await acall_tool_stream_http(
                url=url,
                server_cfg=self.server_cfg,
                token=token,
                timeout=timeout,
                tool_name=tool_name,
                arguments=arguments,
            )
        if transport == "stream_stdio":
            return await acall_tool_stream_stdio(
                server_cfg=self.server_cfg,
                timeout=timeout,
                tool_name=tool_name,
                arguments=arguments,
            )
        if is_persistent_stdio(self.server_cfg):
            # См. `alist_tools`: сессия на `Popen` — сознательное ограничение.
            return await run_abandonable(
                self.call_tool, tool_name, arguments, timeout=timeout
            )

        session_id = await _aget_or_init_session_id(
            self.server_name, url, self.server_cfg, token=token, timeout=timeout
        )
        if not session_id:
            raise RuntimeError(
                f"Failed to initialize MCP session for server '{self.server_name}'"
            )

        async def ado_call(sid: str) -> Any:
            return await _acall_mcp_tool(
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
            data = await ado_call(session_id)
        except httpx.HTTPStatusError:
            forget_session(self.server_name)
            session_id = await _aget_or_init_session_id(
                self.server_name, url, self.server_cfg, token=token, timeout=timeout
            )
            if not session_id:
                raise RuntimeError(
                    f"Failed to re-initialize MCP session for server "
                    f"'{self.server_name}'"
                )
            data = await ado_call(session_id)

        if data == {} and transport == "sse":
            forget_session(self.server_name)
            session_id = await _aget_or_init_session_id(
                self.server_name, url, self.server_cfg, token=token, timeout=timeout
            )
            if session_id:
                data = await ado_call(session_id)
        return data


__all__ = ["McpClientPort", "AgentRuntimeClient"]
