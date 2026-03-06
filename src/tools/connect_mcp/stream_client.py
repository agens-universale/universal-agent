from __future__ import annotations

from typing import Any, Dict, List, Optional

import asyncio

import httpx
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.stdio import StdioServerParameters, stdio_client


def _get_verify_ssl(server_cfg: Dict[str, Any]) -> bool:
    """
    Определить режим проверки SSL-сертификата по конфигу сервера.

    Поддерживает те же варианты, что и transport.get_transport:
      verify-ssl: true/false | "true"/"false"/"0"/"1"/"no"/"off"
    """
    verify_cfg = (server_cfg or {}).get("verify-ssl")
    if isinstance(verify_cfg, bool):
        return verify_cfg
    if isinstance(verify_cfg, str):
        return verify_cfg.strip().lower() not in ("false", "0", "no", "off")
    return True


def _make_httpx_factory(verify: bool):
    """
    Фабрика httpx-клиентов с управляемой проверкой SSL.
    Совместима с сигнатурой httpx_client_factory из MCP SDK.
    """

    def factory(headers: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None, auth: Any = None):
        return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, verify=verify)

    return factory


async def _list_tools_stream_sse_async(
    base_url: str,
    token: Optional[str],
    timeout: float,
    verify: bool,
) -> List[Dict[str, Any]]:
    """
    Асинхронный список тулов через стандартный SSE-транспорт MCP.

    Ожидается, что base_url — базовый URL сервера (например, http://host:port),
    а SSE эндпоинт доступен по base_url + "/sse" (как в официальном примере).
    """
    endpoint = base_url.rstrip("/") + "/sse"

    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    httpx_factory = _make_httpx_factory(verify)

    async with (
        sse_client(
            endpoint,
            headers=headers or None,
            timeout=timeout,
            httpx_client_factory=httpx_factory,
        ) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tools_result = await session.list_tools()

        tools: List[Dict[str, Any]] = []
        for tool in tools_result.tools:
            # В SDK это pydantic-модель; приведём к dict с JSON-совместимыми ключами.
            if hasattr(tool, "model_dump"):
                tool_dict = tool.model_dump(by_alias=True, mode="json", exclude_unset=True)
            elif isinstance(tool, dict):
                tool_dict = tool
            else:
                # Фоллбек на минимальный набор полей
                tool_dict = {
                    "name": getattr(tool, "name", ""),
                    "description": getattr(tool, "description", "") or "",
                }
            tools.append(tool_dict)

        return tools


def list_tools_stream_sse(
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str],
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """
    Синхронная обёртка над _list_tools_stream_sse_async для кода реестра тулов.

    url: базовый URL сервера из конфига (без /sse в конце).
    """
    verify = _get_verify_ssl(server_cfg)
    return asyncio.run(
        _list_tools_stream_sse_async(
            base_url=url,
            token=token,
            timeout=timeout,
            verify=verify,
        )
    )


async def _call_tool_stream_sse_async(
    base_url: str,
    token: Optional[str],
    timeout: float,
    verify: bool,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Асинхронный вызов конкретного MCP-тула через SSE-транспорт.

    Возвращает словарь в формате, совместимом с format_mcp_tool_result:
      {"content": [...], "isError": bool}
    """
    endpoint = base_url.rstrip("/") + "/sse"

    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    httpx_factory = _make_httpx_factory(verify)

    async with (
        sse_client(
            endpoint,
            headers=headers or None,
            timeout=timeout,
            httpx_client_factory=httpx_factory,
        ) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()

        result = await session.call_tool(tool_name, arguments or {})

        # Преобразуем контент в JSON-совместимый вид.
        content_items: List[Any] = []
        for item in getattr(result, "content", []) or []:
            if hasattr(item, "model_dump"):
                content_items.append(item.model_dump(by_alias=True, mode="json", exclude_unset=True))
            else:
                content_items.append(item)

        is_error = bool(getattr(result, "isError", False))

        return {
            "content": content_items,
            "isError": is_error,
        }


def call_tool_stream_sse(
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str],
    timeout: float,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Синхронная обёртка над _call_tool_stream_sse_async для tools/call.

    url: базовый URL сервера из конфига (без /sse в конце).
    """
    verify = _get_verify_ssl(server_cfg)
    return asyncio.run(
        _call_tool_stream_sse_async(
            base_url=url,
            token=token,
            timeout=timeout,
            verify=verify,
            tool_name=tool_name,
            arguments=arguments,
        )
    )


async def _list_tools_stream_http_async(
    base_url: str,
    token: Optional[str],
    timeout: float,
    verify: bool,
) -> List[Dict[str, Any]]:
    """
    Асинхронный список тулов через Streamable HTTP транспорт MCP.
    """
    endpoint = base_url.rstrip("/") + "/mcp"

    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    httpx_factory = _make_httpx_factory(verify)

    async with (
        streamable_http_client(
            endpoint,
            timeout=timeout,
            httpx_client_factory=httpx_factory,
        ) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tools_result = await session.list_tools()

        tools: List[Dict[str, Any]] = []
        for tool in tools_result.tools:
            if hasattr(tool, "model_dump"):
                tool_dict = tool.model_dump(by_alias=True, mode="json", exclude_unset=True)
            elif isinstance(tool, dict):
                tool_dict = tool
            else:
                tool_dict = {
                    "name": getattr(tool, "name", ""),
                    "description": getattr(tool, "description", "") or "",
                }
            tools.append(tool_dict)

        return tools


def list_tools_stream_http(
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str],
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """
    Синхронная обёртка над _list_tools_stream_http_async.

    url: базовый URL сервера из конфига (без /mcp в конце).
    """
    verify = _get_verify_ssl(server_cfg)
    return asyncio.run(
        _list_tools_stream_http_async(
            base_url=url,
            token=token,
            timeout=timeout,
            verify=verify,
        )
    )


async def _call_tool_stream_http_async(
    base_url: str,
    token: Optional[str],
    timeout: float,
    verify: bool,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Асинхронный вызов конкретного MCP-тула через Streamable HTTP транспорт.
    """
    endpoint = base_url.rstrip("/") + "/mcp"

    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    httpx_factory = _make_httpx_factory(verify)

    async with (
        streamable_http_client(
            endpoint,
            timeout=timeout,
            httpx_client_factory=httpx_factory,
        ) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()

        result = await session.call_tool(tool_name, arguments or {})

        content_items: List[Any] = []
        for item in getattr(result, "content", []) or []:
            if hasattr(item, "model_dump"):
                content_items.append(item.model_dump(by_alias=True, mode="json", exclude_unset=True))
            else:
                content_items.append(item)

        is_error = bool(getattr(result, "isError", False))

        return {
            "content": content_items,
            "isError": is_error,
        }


def call_tool_stream_http(
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str],
    timeout: float,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Синхронная обёртка над _call_tool_stream_http_async.

    url: базовый URL сервера из конфига (без /mcp в конце).
    """
    verify = _get_verify_ssl(server_cfg)
    return asyncio.run(
        _call_tool_stream_http_async(
            base_url=url,
            token=token,
            timeout=timeout,
            verify=verify,
            tool_name=tool_name,
            arguments=arguments,
        )
    )


async def _list_tools_stream_stdio_async(
    server_cfg: Dict[str, Any],
    timeout: float,
) -> List[Dict[str, Any]]:
    """
    Асинхронный список тулов через stdio транспорт MCP (через SDK).
    """
    command = server_cfg.get("command")
    args = server_cfg.get("args") or []
    if not isinstance(args, list):
        args = [args]

    params = StdioServerParameters(
        command=str(command),
        args=[str(a) for a in args],
        env=server_cfg.get("env") or None,
        cwd=server_cfg.get("cwd") or None,
    )

    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tools_result = await session.list_tools()

        tools: List[Dict[str, Any]] = []
        for tool in tools_result.tools:
            if hasattr(tool, "model_dump"):
                tool_dict = tool.model_dump(by_alias=True, mode="json", exclude_unset=True)
            elif isinstance(tool, dict):
                tool_dict = tool
            else:
                tool_dict = {
                    "name": getattr(tool, "name", ""),
                    "description": getattr(tool, "description", "") or "",
                }
            tools.append(tool_dict)

        return tools


def list_tools_stream_stdio(
    server_cfg: Dict[str, Any],
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """
    Синхронная обёртка над _list_tools_stream_stdio_async.
    """
    return asyncio.run(
        _list_tools_stream_stdio_async(
            server_cfg=server_cfg,
            timeout=timeout,
        )
    )


async def _call_tool_stream_stdio_async(
    server_cfg: Dict[str, Any],
    timeout: float,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Асинхронный вызов конкретного MCP-тула через stdio транспорт (через SDK).
    """
    command = server_cfg.get("command")
    args = server_cfg.get("args") or []
    if not isinstance(args, list):
        args = [args]

    params = StdioServerParameters(
        command=str(command),
        args=[str(a) for a in args],
        env=server_cfg.get("env") or None,
        cwd=server_cfg.get("cwd") or None,
    )

    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()

        result = await session.call_tool(tool_name, arguments or {})

        content_items: List[Any] = []
        for item in getattr(result, "content", []) or []:
            if hasattr(item, "model_dump"):
                content_items.append(item.model_dump(by_alias=True, mode="json", exclude_unset=True))
            else:
                content_items.append(item)

        is_error = bool(getattr(result, "isError", False))

        return {
            "content": content_items,
            "isError": is_error,
        }


def call_tool_stream_stdio(
    server_cfg: Dict[str, Any],
    timeout: float,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Синхронная обёртка над _call_tool_stream_stdio_async.
    """
    return asyncio.run(
        _call_tool_stream_stdio_async(
            server_cfg=server_cfg,
            timeout=timeout,
            tool_name=tool_name,
            arguments=arguments,
        )
    )

__all__ = [
    "list_tools_stream_sse",
    "call_tool_stream_sse",
    "list_tools_stream_http",
    "call_tool_stream_http",
    "list_tools_stream_stdio",
    "call_tool_stream_stdio",
]


