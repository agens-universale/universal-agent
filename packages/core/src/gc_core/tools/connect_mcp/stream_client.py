from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

import asyncio
from urllib.parse import urlsplit, urlunsplit

from gc_core.utils.url_addr import is_loopback

import httpx
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import McpError

from gc_core.utils.aio_compat import run_coro_blocking
from gc_core.utils.ssl_config import get_verify_ssl, get_client_cert
from gc_core.mcp_runtime.env import mcp_stdio_env
from gc_core.mcp_session_owner import current_owner
from gc_core.tools.connect_mcp.transport import _resolve_command


# ---------------------------------------------------------------------------
# Кэш персистентных MCP-сессий
# ---------------------------------------------------------------------------

class _PersistentSessionManager:
    """Кэш живых ``ClientSession``, ключ — ``(id(loop), server_key)``.

    Раньше здесь крутился ВЕЧНЫЙ event loop в фоновом демон-потоке, а вызовы
    уезжали в него через ``run_coroutine_threadsafe``. Это было нужно, пока
    ядро было синхронным: корутину SDK негде было исполнить. Теперь цикл
    агента асинхронный, и сессия живёт прямо на loop вызывающего.

    Ключ включает ``id(loop)`` по той же причине, что у httpx-клиентов в
    ``utils/async_http.py``: ``ClientSession`` держит открытые соединения,
    привязанные к своему loop. Сессия, открытая на портальном loop и
    переиспользованная на loop приложения, дала бы «Event loop is closed» в
    самый неудобный момент — посреди вызова тула.

    Ключ включает и ВЛАДЕЛЬЦА (см. ``gc_core.mcp_session_owner``). Без него сессии были
    общими для всех прогонов на одном loop, и ``arun`` первого доигравшего
    агента закрывал живые сессии остальных. Для ``stream_*`` это не просто
    потеря состояния, а падение: сессии SDK построены на anyio-scope'ах, вошли
    в них задачи-владельцы, и закрытие из чужой задачи отменяет не ту::

        CancelledError: Cancelled via cancel scope … by <Task pending Task-5>

    Воспроизводилось тремя ``AsyncAgent`` на одном loop, каждый со своим
    браузерным сервером.
    """

    def __init__(self) -> None:
        # (owner, id(loop), server_key) -> открытая ClientSession
        self._sessions: Dict[tuple, Any] = {}
        # (owner, id(loop), server_key) -> стек контекст-менеджеров для закрытия
        self._cm_stacks: Dict[tuple, Any] = {}

    @staticmethod
    def _loop_id() -> int:
        return id(asyncio.get_running_loop())

    def _key(self, server_key: str) -> tuple:
        return (current_owner(), self._loop_id(), server_key)

    def get_session(self, server_key: str) -> Optional[Any]:
        try:
            return self._sessions.get(self._key(server_key))
        except RuntimeError:
            # Живого loop нет — значит и сессии на нём быть не может.
            return None

    def set_session(self, server_key: str, session: Any, cm_stack: Any) -> None:
        key = self._key(server_key)
        self._sessions[key] = session
        self._cm_stacks[key] = cm_stack

    def invalidate(self, server_key: str) -> None:
        """Забыть сессию (после ошибки). Стек НЕ закрываем: loop может быть чужим."""
        try:
            key = self._key(server_key)
        except RuntimeError:
            return
        self._sessions.pop(key, None)
        self._cm_stacks.pop(key, None)

    async def _aclose_stack(self, cms: Any) -> None:
        # cm_stack — кортеж контекст-менеджеров в порядке открытия;
        # закрываем в обратном, глотая ошибку по каждому.
        for cm in reversed(cms):
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass

    async def aclose_all(self) -> None:
        """Закрыть сессии ЭТОГО владельца на ЭТОМ loop.

        Оба условия обязательны. Чужой loop — закрывать нечем (соединения
        привязаны к своему). Чужой владелец — сессия ещё РАБОТАЕТ: параллельный
        агент в этот момент внутри вызова тула, и закрытие из нашей задачи
        отменит его anyio-scope.
        """
        try:
            loop_id = self._loop_id()
        except RuntimeError:
            return
        owner = current_owner()
        keys = [k for k in self._cm_stacks if k[0] == owner and k[1] == loop_id]
        for key in keys:
            cms = self._cm_stacks.pop(key, None)
            self._sessions.pop(key, None)
            if cms is not None:
                await self._aclose_stack(cms)

    def close_all(self) -> None:
        """Отпустить ВСЕ сессии, включая чужие. Для завершения процесса и тестов.

        Если loop живёт на этом потоке, звать нельзя: получился бы вложенный
        ``asyncio.run``. Такой вызывающий обязан использовать ``aclose_all``.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "close_all() вызван из работающего event loop; используйте "
                "await aclose_all()"
            )
        if not self._cm_stacks:
            return
        # Сессии принадлежат уже мёртвым loop: их корректно закрыть нечем,
        # просто отпускаем ссылки, чтобы не течь.
        self._sessions.clear()
        self._cm_stacks.clear()


_SESSION_MANAGER = _PersistentSessionManager()

def _is_loopback_url(url: str) -> bool:
    """True для локальных MCP-daemon'ов (127.0.0.1, localhost, ::1).

    Делегирует в `gc_core.utils.url_addr.is_loopback`. Своя копия проверки жила
    здесь и содержала `hostname.startswith("127.")` — под неё подходил
    `http://127.0.0.1.evil.com/mcp`, обычное DNS-имя. Следствие ниже, в
    `_trust_env_for_url`: адрес признавался локальным, снимался `trust_env`, и
    запрос с `Authorization: Bearer` уходил напрямую, минуя корпоративный прокси.

    Канон в `url_addr` разбирает адрес через `ipaddress`, а не по префиксу
    строки; он был написан ровно против этой дыры, но переключить сюда забыли.
    """
    return is_loopback(url)


def _trust_env_for_url(url: str) -> bool:
    """Локальные MCP не должны ходить через HTTP_PROXY/socks5h из shell."""
    return not _is_loopback_url(url)


def _merge_mcp_headers(
    token: Optional[str],
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Собрать финальные HTTP headers для MCP запросов.

    Правило приоритета:
    - Authorization из `token` добавляется первой (если задан `token`)
    - затем накладываются `extra_headers` (они могут переопределить Authorization).
    """
    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        for k, v in extra_headers.items():
            headers[str(k)] = str(v)
    return headers


def _make_httpx_factory(
    verify: Union[bool, str],
    cert: Optional[Tuple[str, str]] = None,
    *,
    trust_env: bool = True,
):
    """
    Фабрика httpx-клиентов с управляемой проверкой SSL и клиентским сертификатом.
    Совместима с сигнатурой httpx_client_factory из MCP SDK.
    """

    def factory(headers: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None, auth: Any = None):
        kwargs: Dict[str, Any] = dict(
            headers=headers,
            timeout=timeout,
            auth=auth,
            verify=httpx.create_ssl_context(verify=verify),
            trust_env=trust_env,
        )
        if cert is not None:
            kwargs["cert"] = cert
        return httpx.AsyncClient(**kwargs)

    return factory


def _make_stream_http_client(
    token: Optional[str],
    extra_headers: Optional[Dict[str, str]],
    timeout: float,
    verify: Union[bool, str],
    cert: Optional[Tuple[str, str]] = None,
    *,
    base_url: str = "",
) -> httpx.AsyncClient:
    """
    Создать httpx.AsyncClient для stream_http (MCP 1.26.0).
    В этой версии streamable_http_client принимает http_client и
    не принимает headers/timeout/httpx_client_factory напрямую.
    """
    headers = _merge_mcp_headers(token=token, extra_headers=extra_headers)

    kwargs: Dict[str, Any] = dict(
        headers=headers or None,
        timeout=timeout,
        verify=httpx.create_ssl_context(verify=verify),
        trust_env=_trust_env_for_url(base_url),
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
    extra_headers: Optional[Dict[str, str]],
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

    headers = _merge_mcp_headers(token=token, extra_headers=extra_headers)

    httpx_factory = _make_httpx_factory(
        verify,
        cert=cert,
        trust_env=_trust_env_for_url(base_url),
    )

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


async def alist_tools_stream_sse(
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str],
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """Список тулов по SSE, не занимая поток.

    url: базовый URL сервера из конфига (без /sse в конце).
    """
    verify = get_verify_ssl(server_cfg)
    cert = get_client_cert(server_cfg)
    extra_headers = server_cfg.get("headers") or None
    return await _list_tools_stream_sse_async(
        base_url=url,
        token=token,
        extra_headers=extra_headers,
        timeout=timeout,
        verify=verify,
        cert=cert,
    )


def list_tools_stream_sse(
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str],
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """Синхронный двойник. Реализация одна — асинхронная."""
    return run_coro_blocking(
        alist_tools_stream_sse(url, server_cfg, token, timeout)
    )


async def _call_tool_stream_sse_async(
    base_url: str,
    token: Optional[str],
    extra_headers: Optional[Dict[str, str]],
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

    headers = _merge_mcp_headers(token=token, extra_headers=extra_headers)

    httpx_factory = _make_httpx_factory(
        verify,
        cert=cert,
        trust_env=_trust_env_for_url(base_url),
    )

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
    extra_headers: Optional[Dict[str, str]],
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
        headers = _merge_mcp_headers(token=token, extra_headers=extra_headers)
        httpx_factory = _make_httpx_factory(
            verify,
            cert=cert,
            trust_env=_trust_env_for_url(base_url),
        )
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


async def acall_tool_stream_sse(
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str],
    timeout: float,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Вызов тула по SSE, не занимая поток.

    Если server_cfg.persistent_session=true — persistent режим (сессия живёт
    между вызовами). Иначе stateless.

    url: базовый URL сервера из конфига (без /sse в конце).
    """
    verify = get_verify_ssl(server_cfg)
    cert = get_client_cert(server_cfg)
    extra_headers = server_cfg.get("headers") or None

    if bool(server_cfg.get("persistent_session")):
        server_key = f"stream_sse::{url}"
        try:
            return await _call_tool_stream_sse_persistent(
                server_key=server_key,
                base_url=url,
                token=token,
                extra_headers=extra_headers,
                timeout=timeout,
                verify=verify,
                tool_name=tool_name,
                arguments=arguments,
                cert=cert,
            )
        except Exception:
            _SESSION_MANAGER.invalidate(server_key)

    return await _call_tool_stream_sse_async(
        base_url=url,
        token=token,
        extra_headers=extra_headers,
        timeout=timeout,
        verify=verify,
        tool_name=tool_name,
        arguments=arguments,
        cert=cert,
    )


def call_tool_stream_sse(
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str],
    timeout: float,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Синхронный двойник. Реализация одна — асинхронная."""
    return run_coro_blocking(
        acall_tool_stream_sse(url, server_cfg, token, timeout, tool_name, arguments)
    )


async def _list_tools_stream_http_async(
    base_url: str,
    token: Optional[str],
    extra_headers: Optional[Dict[str, str]],
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
                    extra_headers=extra_headers,
                    timeout=timeout,
                    verify=verify,
                    cert=cert,
                    base_url=base_url,
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


async def alist_tools_stream_http(
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str],
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """Список тулов по streamable HTTP, не занимая поток.

    url: базовый URL сервера из конфига (без /mcp в конце).
    """
    verify = get_verify_ssl(server_cfg)
    cert = get_client_cert(server_cfg)
    extra_headers = server_cfg.get("headers") or None
    return await _list_tools_stream_http_async(
        base_url=url,
        token=token,
        extra_headers=extra_headers,
        timeout=timeout,
        verify=verify,
        cert=cert,
    )


def list_tools_stream_http(
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str],
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """Синхронный двойник. Реализация одна — асинхронная."""
    return run_coro_blocking(
        alist_tools_stream_http(url, server_cfg, token, timeout)
    )


async def _call_tool_stream_http_async(
    base_url: str,
    token: Optional[str],
    extra_headers: Optional[Dict[str, str]],
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
                    extra_headers=extra_headers,
                    timeout=timeout,
                    verify=verify,
                    cert=cert,
                    base_url=base_url,
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
    extra_headers: Optional[Dict[str, str]],
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
        http_client = _make_stream_http_client(
            token=token,
            extra_headers=extra_headers,
            timeout=timeout,
            verify=verify,
            cert=cert,
            base_url=base_url,
        )
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


async def acall_tool_stream_http(
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str],
    timeout: float,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Вызов тула по streamable HTTP, не занимая поток.

    Персистентная сессия включается флагом `persistent_session: true`
    (единый дефолт со всеми остальными транспортами — False).

    url: базовый URL сервера из конфига (без /mcp в конце).
    """
    verify = get_verify_ssl(server_cfg)
    cert = get_client_cert(server_cfg)
    extra_headers = server_cfg.get("headers") or None
    server_key = f"stream_http::{url}"

    async def _stateless() -> Dict[str, Any]:
        return await _call_tool_stream_http_async(
            base_url=url,
            token=token,
            extra_headers=extra_headers,
            timeout=timeout,
            verify=verify,
            tool_name=tool_name,
            arguments=arguments,
            cert=cert,
        )

    if not bool(server_cfg.get("persistent_session")):
        return await _stateless()

    try:
        return await _call_tool_stream_http_persistent(
            server_key=server_key,
            base_url=url,
            token=token,
            extra_headers=extra_headers,
            timeout=timeout,
            verify=verify,
            tool_name=tool_name,
            arguments=arguments,
            cert=cert,
        )
    except Exception:
        # Персистентная сессия не задалась — забываем её и идём stateless.
        _SESSION_MANAGER.invalidate(server_key)
        return await _stateless()


def call_tool_stream_http(
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str],
    timeout: float,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Синхронный двойник. Реализация одна — асинхронная."""
    return run_coro_blocking(
        acall_tool_stream_http(url, server_cfg, token, timeout, tool_name, arguments)
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
        env=mcp_stdio_env(server_cfg.get("env") or None),
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


async def alist_tools_stream_stdio(
    server_cfg: Dict[str, Any],
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """Список тулов по stdio (через SDK), не занимая поток."""
    return await _list_tools_stream_stdio_async(
        server_cfg=server_cfg,
        timeout=timeout,
    )


def list_tools_stream_stdio(
    server_cfg: Dict[str, Any],
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """Синхронный двойник. Реализация одна — асинхронная."""
    return run_coro_blocking(alist_tools_stream_stdio(server_cfg, timeout))


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
        env=mcp_stdio_env(server_cfg.get("env") or None),
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
            env=mcp_stdio_env(server_cfg.get("env") or None),
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


async def acall_tool_stream_stdio(
    server_cfg: Dict[str, Any],
    timeout: float,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Вызов тула по stdio (через SDK), не занимая поток.

    При persistent_session=true субпроцесс и ClientSession держатся живыми
    между вызовами (одна Chromium-сессия на весь цикл работы агента).
    """
    if bool(server_cfg.get("persistent_session")):
        # Ключ по команде+args, чтобы одинаковые сервера делили сессию.
        args = server_cfg.get("args") or []
        args_key = ",".join(str(a) for a in (args if isinstance(args, list) else [args]))
        server_key = f"stream_stdio::{server_cfg.get('command')}::{args_key}"
        try:
            return await _call_tool_stream_stdio_persistent(
                server_key=server_key,
                server_cfg=server_cfg,
                timeout=timeout,
                tool_name=tool_name,
                arguments=arguments,
            )
        except Exception:
            _SESSION_MANAGER.invalidate(server_key)

    return await _call_tool_stream_stdio_async(
        server_cfg=server_cfg,
        timeout=timeout,
        tool_name=tool_name,
        arguments=arguments,
    )


def call_tool_stream_stdio(
    server_cfg: Dict[str, Any],
    timeout: float,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Синхронный двойник. Реализация одна — асинхронная."""
    return run_coro_blocking(
        acall_tool_stream_stdio(server_cfg, timeout, tool_name, arguments)
    )


__all__ = [
    "acall_tool_stream_http",
    "acall_tool_stream_sse",
    "acall_tool_stream_stdio",
    "alist_tools_stream_http",
    "alist_tools_stream_sse",
    "alist_tools_stream_stdio",
    "list_tools_stream_sse",
    "call_tool_stream_sse",
    "list_tools_stream_http",
    "call_tool_stream_http",
    "list_tools_stream_stdio",
    "call_tool_stream_stdio",
]


