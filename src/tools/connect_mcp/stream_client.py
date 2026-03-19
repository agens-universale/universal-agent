from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import asyncio
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import McpError

from utils.ssl_config import get_verify_ssl, get_client_cert


def _make_httpx_factory(verify: bool, cert: Optional[Tuple[str, str]] = None):
    """
    Фабрика httpx-клиентов с управляемой проверкой SSL и клиентским сертификатом.
    Совместима с сигнатурой httpx_client_factory из MCP SDK.
    """

    def factory(headers: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None, auth: Any = None):
        kwargs: Dict[str, Any] = dict(headers=headers, timeout=timeout, auth=auth, verify=verify)
        if cert is not None:
            kwargs["cert"] = cert
        return httpx.AsyncClient(**kwargs)

    return factory


def _make_stream_http_client(
    token: Optional[str],
    timeout: float,
    verify: bool,
    cert: Optional[Tuple[str, str]] = None,
) -> httpx.AsyncClient:
    """
    Создать httpx.AsyncClient для stream_http (MCP 1.26.0).
    В этой версии streamable_http_client принимает http_client и
    не принимает headers/timeout/httpx_client_factory напрямую.
    """
    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    kwargs: Dict[str, Any] = dict(
        headers=headers or None,
        timeout=timeout,
        verify=verify,
    )
    if cert is not None:
        kwargs["cert"] = cert

    return httpx.AsyncClient(**kwargs)


def _build_endpoint(base_url: str, suffix: str) -> str:
    """
    Построить endpoint без дублирования суффикса.
    Пример: base_url="https://host/mcp", suffix="/mcp" -> "https://host/mcp"
    """
    base = (base_url or "").strip()
    if not base:
        return suffix

    parts = urlsplit(base)
    path = (parts.path or "").rstrip("/")
    clean_suffix = suffix.rstrip("/")

    if path.endswith(clean_suffix):
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))

    new_path = f"{path}{clean_suffix}" if path else clean_suffix
    return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, parts.fragment))


def _stream_http_endpoints(base_url: str) -> List[str]:
    """
    Кандидаты endpoint для stream_http.
    Сначала пробуем <base>/mcp (дефолт по старой логике клиента),
    при 404(Session terminated) — fallback на исходный base_url.
    """
    with_mcp = _build_endpoint(base_url, "/mcp")
    raw = (base_url or "").strip().rstrip("/")
    if raw and raw != with_mcp:
        return [with_mcp, raw]
    return [with_mcp]


async def _list_tools_stream_sse_async(
    base_url: str,
    token: Optional[str],
    timeout: float,
    verify: bool,
    cert: Optional[Tuple[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Асинхронный список тулов через стандартный SSE-транспорт MCP.

    Ожидается, что base_url — базовый URL сервера (например, http://host:port),
    а SSE эндпоинт доступен по base_url + "/sse" (как в официальном примере).
    """
    endpoint = _build_endpoint(base_url, "/sse")

    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    httpx_factory = _make_httpx_factory(verify, cert=cert)

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
    verify = get_verify_ssl(server_cfg)
    cert = get_client_cert(server_cfg)
    return asyncio.run(
        _list_tools_stream_sse_async(
            base_url=url,
            token=token,
            timeout=timeout,
            verify=verify,
            cert=cert,
        )
    )


async def _call_tool_stream_sse_async(
    base_url: str,
    token: Optional[str],
    timeout: float,
    verify: bool,
    tool_name: str,
    arguments: Dict[str, Any],
    cert: Optional[Tuple[str, str]] = None,
) -> Dict[str, Any]:
    """
    Асинхронный вызов конкретного MCP-тула через SSE-транспорт.

    Возвращает словарь в формате, совместимом с format_mcp_tool_result:
      {"content": [...], "isError": bool}
    """
    endpoint = _build_endpoint(base_url, "/sse")

    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    httpx_factory = _make_httpx_factory(verify, cert=cert)

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
    verify = get_verify_ssl(server_cfg)
    cert = get_client_cert(server_cfg)
    return asyncio.run(
        _call_tool_stream_sse_async(
            base_url=url,
            token=token,
            timeout=timeout,
            verify=verify,
            tool_name=tool_name,
            arguments=arguments,
            cert=cert,
        )
    )


async def _list_tools_stream_http_async(
    base_url: str,
    token: Optional[str],
    timeout: float,
    verify: bool,
    cert: Optional[Tuple[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Асинхронный список тулов через Streamable HTTP транспорт MCP.
    """
    endpoints = _stream_http_endpoints(base_url)
    last_error: Optional[Exception] = None

    for endpoint in endpoints:
        try:
            async with (
                _make_stream_http_client(
                    token=token,
                    timeout=timeout,
                    verify=verify,
                    cert=cert,
                ) as http_client,
                streamable_http_client(
                    endpoint,
                    http_client=http_client,
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
        except McpError as exc:
            last_error = exc
            is_session_terminated = "Session terminated" in str(exc)
            has_fallback = endpoint != endpoints[-1]
            if is_session_terminated and has_fallback:
                continue
            raise

    if last_error is not None:
        raise last_error
    return []


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
    verify = get_verify_ssl(server_cfg)
    cert = get_client_cert(server_cfg)
    return asyncio.run(
        _list_tools_stream_http_async(
            base_url=url,
            token=token,
            timeout=timeout,
            verify=verify,
            cert=cert,
        )
    )


async def _call_tool_stream_http_async(
    base_url: str,
    token: Optional[str],
    timeout: float,
    verify: bool,
    tool_name: str,
    arguments: Dict[str, Any],
    cert: Optional[Tuple[str, str]] = None,
) -> Dict[str, Any]:
    """
    Асинхронный вызов конкретного MCP-тула через Streamable HTTP транспорт.
    """
    endpoints = _stream_http_endpoints(base_url)
    last_error: Optional[Exception] = None

    for endpoint in endpoints:
        try:
            async with (
                _make_stream_http_client(
                    token=token,
                    timeout=timeout,
                    verify=verify,
                    cert=cert,
                ) as http_client,
                streamable_http_client(
                    endpoint,
                    http_client=http_client,
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
        except McpError as exc:
            last_error = exc
            is_session_terminated = "Session terminated" in str(exc)
            has_fallback = endpoint != endpoints[-1]
            if is_session_terminated and has_fallback:
                continue
            raise

    if last_error is not None:
        raise last_error
    return {"content": [], "isError": True}


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
    verify = get_verify_ssl(server_cfg)
    cert = get_client_cert(server_cfg)
    return asyncio.run(
        _call_tool_stream_http_async(
            base_url=url,
            token=token,
            timeout=timeout,
            verify=verify,
            tool_name=tool_name,
            arguments=arguments,
            cert=cert,
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


