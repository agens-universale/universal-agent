from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

import asyncio
import threading
import concurrent.futures
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import McpError

from utils.ssl_config import get_verify_ssl, get_client_cert
from tools.connect_mcp.transport import _resolve_command


# ---------------------------------------------------------------------------
# Persistent session manager
# Keeps one MCP ClientSession alive per server key in a background event loop.
# This lets browser (and other stateful) MCP servers maintain context across
# multiple tool calls within a single gc-cli process.
# ---------------------------------------------------------------------------

class _PersistentSessionManager:
    """
    Runs a dedicated asyncio event loop in a background daemon thread.
    Caches open MCP ClientSession objects keyed by server_key.
    All async calls are dispatched to that loop via run_coroutine_threadsafe.
    """

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # server_key -> open ClientSession (async object in _loop)
        self._sessions: Dict[str, Any] = {}
        # server_key -> context managers stack (for cleanup)
        self._cm_stacks: Dict[str, Any] = {}

    def _ensure_loop(self):
        with self._lock:
            if self._loop is None or not self._loop.is_running():
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever,
                    daemon=True,
                    name="mcp-session-loop",
                )
                self._thread.start()

    def run(self, coro) -> Any:
        """Submit a coroutine to the background loop and block until done."""
        self._ensure_loop()
        fut = concurrent.futures.Future()

        async def _wrapper():
            try:
                result = await coro
                fut.set_result(result)
            except Exception as exc:
                fut.set_exception(exc)

        asyncio.run_coroutine_threadsafe(_wrapper(), self._loop)
        return fut.result(timeout=120)

    def get_session(self, server_key: str) -> Optional[Any]:
        return self._sessions.get(server_key)

    def set_session(self, server_key: str, session: Any, cm_stack: Any):
        self._sessions[server_key] = session
        self._cm_stacks[server_key] = cm_stack

    def invalidate(self, server_key: str):
        """Remove cached session (e.g. after an error)."""
        self._sessions.pop(server_key, None)
        self._cm_stacks.pop(server_key, None)

    def close_all(self) -> None:
        """
        Закрыть все открытые сессии и остановить фоновый loop.
        Безопасно вызывать из основного потока; каждый cm_stack закрывается
        в своём же event loop через run_coroutine_threadsafe.
        Ничего не делает, если loop не запускался.
        """
        with self._lock:
            loop = self._loop
            stacks = list(self._cm_stacks.items())
            self._sessions.clear()
            self._cm_stacks.clear()

        if loop is None or not loop.is_running():
            return

        async def _close_stack(cms: Any) -> None:
            # cm_stack — tuple контекст-менеджеров в порядке открытия;
            # закрываем в обратном порядке, глотая ошибки по каждому.
            for cm in reversed(cms):
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass

        for _key, cm_stack in stacks:
            fut = concurrent.futures.Future()

            async def _wrapper(cms=cm_stack):
                try:
                    await _close_stack(cms)
                    fut.set_result(None)
                except Exception as exc:
                    fut.set_exception(exc)

            try:
                asyncio.run_coroutine_threadsafe(_wrapper(), loop)
                fut.result(timeout=15)
            except Exception:
                pass

        # Останавливаем loop и даём фоновому потоку завершиться.
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        thread = self._thread
        if thread is not None:
            try:
                thread.join(timeout=5)
            except Exception:
                pass

        with self._lock:
            if self._loop is loop:
                self._loop = None
                self._thread = None


_SESSION_MANAGER = _PersistentSessionManager()


def _make_httpx_factory(verify: Union[bool, str], cert: Optional[Tuple[str, str]] = None):
    """
    Фабрика httpx-клиентов с управляемой проверкой SSL и клиентским сертификатом.
    Совместима с сигнатурой httpx_client_factory из MCP SDK.
    """

    def factory(headers: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None, auth: Any = None):
        kwargs: Dict[str, Any] = dict(
            headers=headers,
            timeout=timeout,
            auth=auth,
            verify=httpx.create_ssl_context(verify=verify)
        )
        if cert is not None:
            kwargs["cert"] = cert
        return httpx.AsyncClient(**kwargs)

    return factory


def _make_stream_http_client(
    token: Optional[str],
    timeout: float,
    verify: Union[bool, str],
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
        verify=httpx.create_ssl_context(verify=verify),
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
    verify: Union[bool, str],
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
    verify: Union[bool, str],
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


async def _call_tool_stream_sse_persistent(
    server_key: str,
    base_url: str,
    token: Optional[str],
    timeout: float,
    verify: Union[bool, str],
    tool_name: str,
    arguments: Dict[str, Any],
    cert: Optional[Tuple[str, str]] = None,
) -> Dict[str, Any]:
    """
    Persistent вариант: держим открытой одну ClientSession поверх живого
    SSE-коннекта в фоновом event loop _SESSION_MANAGER, переиспользуем между вызовами.
    """
    session: Optional[ClientSession] = _SESSION_MANAGER.get_session(server_key)

    async def _open_session() -> ClientSession:
        endpoint = _build_endpoint(base_url, "/sse")
        headers: Dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        httpx_factory = _make_httpx_factory(verify, cert=cert)
        transport_cm = sse_client(
            endpoint,
            headers=headers or None,
            timeout=timeout,
            httpx_client_factory=httpx_factory,
        )
        read_stream, write_stream = await transport_cm.__aenter__()
        new_session = ClientSession(read_stream, write_stream)
        await new_session.__aenter__()
        await new_session.initialize()
        _SESSION_MANAGER.set_session(server_key, new_session, (transport_cm, new_session))
        return new_session

    async def _call(s: ClientSession) -> Dict[str, Any]:
        result = await s.call_tool(tool_name, arguments or {})
        content_items: List[Any] = []
        for item in getattr(result, "content", []) or []:
            if hasattr(item, "model_dump"):
                content_items.append(item.model_dump(by_alias=True, mode="json", exclude_unset=True))
            else:
                content_items.append(item)
        return {"content": content_items, "isError": bool(getattr(result, "isError", False))}

    if session is not None:
        try:
            return await _call(session)
        except Exception:
            _SESSION_MANAGER.invalidate(server_key)
            session = None

    session = await _open_session()
    return await _call(session)


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

    Если server_cfg.persistent_session=true — используется persistent режим
    (сессия живёт между вызовами). Иначе — stateless (как раньше).

    url: базовый URL сервера из конфига (без /sse в конце).
    """
    verify = get_verify_ssl(server_cfg)
    cert = get_client_cert(server_cfg)

    if bool(server_cfg.get("persistent_session")):
        server_key = f"stream_sse::{url}"
        try:
            return _SESSION_MANAGER.run(
                _call_tool_stream_sse_persistent(
                    server_key=server_key,
                    base_url=url,
                    token=token,
                    timeout=timeout,
                    verify=verify,
                    tool_name=tool_name,
                    arguments=arguments,
                    cert=cert,
                )
            )
        except Exception:
            _SESSION_MANAGER.invalidate(f"stream_sse::{url}")

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
    verify: Union[bool, str],
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
    verify: Union[bool, str],
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


async def _call_tool_stream_http_persistent(
    server_key: str,
    base_url: str,
    token: Optional[str],
    timeout: float,
    verify: Union[bool, str],
    tool_name: str,
    arguments: Dict[str, Any],
    cert: Optional[Tuple[str, str]] = None,
) -> Dict[str, Any]:
    """
    Вызов MCP-тула через Streamable HTTP с персистентной сессией.
    Сессия кешируется в _SESSION_MANAGER и переиспользуется между вызовами.
    """
    session: Optional[ClientSession] = _SESSION_MANAGER.get_session(server_key)

    async def _open_session(endpoint: str) -> ClientSession:
        http_client = _make_stream_http_client(token=token, timeout=timeout, verify=verify, cert=cert)
        await http_client.__aenter__()
        transport = streamable_http_client(endpoint, http_client=http_client)
        read_stream, write_stream, _ = await transport.__aenter__()
        new_session = ClientSession(read_stream, write_stream)
        await new_session.__aenter__()
        await new_session.initialize()
        _SESSION_MANAGER.set_session(server_key, new_session, (http_client, transport, new_session))
        return new_session

    endpoints = _stream_http_endpoints(base_url)

    async def _call(s: ClientSession) -> Dict[str, Any]:
        result = await s.call_tool(tool_name, arguments or {})
        content_items: List[Any] = []
        for item in getattr(result, "content", []) or []:
            if hasattr(item, "model_dump"):
                content_items.append(item.model_dump(by_alias=True, mode="json", exclude_unset=True))
            else:
                content_items.append(item)
        return {"content": content_items, "isError": bool(getattr(result, "isError", False))}

    # Try with cached session first
    if session is not None:
        try:
            return await _call(session)
        except Exception:
            _SESSION_MANAGER.invalidate(server_key)
            session = None

    # Open new persistent session
    last_error: Optional[Exception] = None
    for endpoint in endpoints:
        try:
            session = await _open_session(endpoint)
            return await _call(session)
        except McpError as exc:
            last_error = exc
            _SESSION_MANAGER.invalidate(server_key)
            if "Session terminated" in str(exc) and endpoint != endpoints[-1]:
                continue
            raise
        except Exception as exc:
            last_error = exc
            _SESSION_MANAGER.invalidate(server_key)

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
    Персистентная сессия включается флагом `persistent_session: true`
    (единый дефолт со всеми остальными транспортами — False).

    url: базовый URL сервера из конфига (без /mcp в конце).
    """
    verify = get_verify_ssl(server_cfg)
    cert = get_client_cert(server_cfg)
    server_key = f"stream_http::{url}"

    persistent = bool(server_cfg.get("persistent_session"))
    if not persistent:
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

    try:
        return _SESSION_MANAGER.run(
            _call_tool_stream_http_persistent(
                server_key=server_key,
                base_url=url,
                token=token,
                timeout=timeout,
                verify=verify,
                tool_name=tool_name,
                arguments=arguments,
                cert=cert,
            )
        )
    except Exception:
        # Fallback to stateless mode on any persistent session failure
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
        command=_resolve_command(str(command)),
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
        command=_resolve_command(str(command)),
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


async def _call_tool_stream_stdio_persistent(
    server_key: str,
    server_cfg: Dict[str, Any],
    timeout: float,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Persistent вариант: держим stdio MCP-сервер живым (один subprocess +
    одна ClientSession) в фоновом event loop _SESSION_MANAGER.
    """
    session: Optional[ClientSession] = _SESSION_MANAGER.get_session(server_key)

    async def _open_session() -> ClientSession:
        command = server_cfg.get("command")
        args = server_cfg.get("args") or []
        if not isinstance(args, list):
            args = [args]
        params = StdioServerParameters(
            command=_resolve_command(str(command)),
            args=[str(a) for a in args],
            env=server_cfg.get("env") or None,
            cwd=server_cfg.get("cwd") or None,
        )
        transport_cm = stdio_client(params)
        read_stream, write_stream = await transport_cm.__aenter__()
        new_session = ClientSession(read_stream, write_stream)
        await new_session.__aenter__()
        await new_session.initialize()
        _SESSION_MANAGER.set_session(server_key, new_session, (transport_cm, new_session))
        return new_session

    async def _call(s: ClientSession) -> Dict[str, Any]:
        result = await s.call_tool(tool_name, arguments or {})
        content_items: List[Any] = []
        for item in getattr(result, "content", []) or []:
            if hasattr(item, "model_dump"):
                content_items.append(item.model_dump(by_alias=True, mode="json", exclude_unset=True))
            else:
                content_items.append(item)
        return {"content": content_items, "isError": bool(getattr(result, "isError", False))}

    if session is not None:
        try:
            return await _call(session)
        except Exception:
            _SESSION_MANAGER.invalidate(server_key)
            session = None

    session = await _open_session()
    return await _call(session)


def call_tool_stream_stdio(
    server_cfg: Dict[str, Any],
    timeout: float,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Синхронная обёртка над _call_tool_stream_stdio_async.

    При persistent_session=true субпроцесс и ClientSession держатся живыми
    между вызовами (одна Chromium-сессия на весь цикл работы агента).
    """
    if bool(server_cfg.get("persistent_session")):
        # Ключ по команде+args, чтобы одинаковые сервера делили сессию.
        args = server_cfg.get("args") or []
        args_key = ",".join(str(a) for a in (args if isinstance(args, list) else [args]))
        server_key = f"stream_stdio::{server_cfg.get('command')}::{args_key}"
        try:
            return _SESSION_MANAGER.run(
                _call_tool_stream_stdio_persistent(
                    server_key=server_key,
                    server_cfg=server_cfg,
                    timeout=timeout,
                    tool_name=tool_name,
                    arguments=arguments,
                )
            )
        except Exception:
            _SESSION_MANAGER.invalidate(server_key)

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


