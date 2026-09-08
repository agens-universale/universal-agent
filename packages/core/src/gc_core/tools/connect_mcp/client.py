from typing import Any, Dict, List, Optional

from gc_core.tools.connect_mcp.transport import aget_transport, get_transport


# Параметры `initialize`, общие для всех локусов: один источник, иначе при подъёме
# версии протокола агентский и контейнерный путь разъедутся молча.
MCP_INIT_PARAMS: Dict[str, Any] = {
    "protocolVersion": "2024-11-05",
    "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
    "clientInfo": {"name": "SurfaceClient", "version": "1.0.0"},
}

# Кэш MCP-сессий в рамках процесса:
# ключ — имя сервера из конфига (server_name), значение — session_id.
#: MCP session-id по ключу ``(владелец, server_key)``.
#:
#: Владелец здесь по той же причине, что и в двух других реестрах: session-id
#: адресует состояние НА СЕРВЕРЕ (для браузерного MCP — конкретный контекст со
#: своими вкладками и куками). Один id на всех агентов означал бы, что они
#: молча работают в одной вкладке и сбивают друг другу страницы. Teardown'а
#: здесь нет, поэтому падения не было — была тихая общая песочница.
from gc_core.mcp_session_owner import current_owner

_MCP_SESSIONS: Dict[tuple, str] = {}


def _merge_mcp_headers(
    *,
    token: Optional[str],
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Собрать финальные HTTP headers для MCP запросов.

    Правило приоритета:
    - Authorization из `token` добавляется первой (если `token` задан)
    - затем накладываются `extra_headers` (они могут переопределить Authorization).
    """
    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        for k, v in extra_headers.items():
            headers[str(k)] = str(v)
    return headers


def _normalize_url(url: str) -> str:
    """
    Убедиться, что в URL есть схема (http/https).
    """
    if not url.startswith("http://") and not url.startswith("https://"):
        return f"http://{url}"
    return url



#: Спека streamable HTTP требует готовности принять ОБА типа: сервер вправе
#: ответить и JSON, и SSE. Только "application/json" даёт 406 Not Acceptable у
#: совместимой реализации (проверено на FastMCP).
_ACCEPT_BOTH = "application/json, text/event-stream"


def _mcp_headers(
    server_cfg: Dict[str, Any],
    token: Optional[str],
    *,
    session_id: Optional[str] = None,
    content_type: bool = True,
) -> Dict[str, str]:
    """Заголовки одного вызова MCP. Один источник для всех методов и обеих форм.

    Раньше эти пять строк были скопированы в каждую функцию модуля, и правка
    ``Accept`` (тот самый 406) чинилась в них по одной.
    """
    headers: Dict[str, str] = {"Accept": _ACCEPT_BOTH}
    if content_type:
        headers["Content-Type"] = "application/json"
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    extra_headers: Dict[str, str] = server_cfg.get("headers") or {}
    headers.update(_merge_mcp_headers(token=token, extra_headers=extra_headers))
    return headers


def _session_id_from(resp_headers: Dict[str, str]) -> Optional[str]:
    return resp_headers.get("mcp-session-id") or resp_headers.get("MCP-Session-Id")


def _tools_from(data: Any) -> List[Dict[str, Any]]:
    """Тело tools/list → список тулов.

    Ожидаемый формат JSON-RPC 2.0:
    ``{"jsonrpc": "2.0", "id": "...", "result": {"tools": [...]}}``
    """
    if isinstance(data, dict):
        if isinstance(data.get("result"), dict):
            tools = data["result"].get("tools", [])
        else:
            tools = data.get("tools", [])
    else:
        tools = []
    return tools if isinstance(tools, list) else []


_NOTIFICATION_PAYLOAD: Dict[str, Any] = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
}


def _initialize_payload() -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": MCP_INIT_PARAMS,
    }


def _tools_list_payload() -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "tools-list-1",
        "method": "tools/list",
        "params": {},
    }


def _tools_call_payload(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "tools-call-1",
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }


def _send_initialized_notification(
    url: str,
    session_id: str,
    server_cfg: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> None:
    """Отправить notifications/initialized после успешного initialize."""
    headers = _mcp_headers(server_cfg, token, session_id=session_id)
    get_transport(server_cfg)(url, headers, _NOTIFICATION_PAYLOAD, timeout)


async def _asend_initialized_notification(
    url: str,
    session_id: str,
    server_cfg: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> None:
    """Асинхронный двойник: отличается ровно транспортом."""
    headers = _mcp_headers(server_cfg, token, session_id=session_id)
    await aget_transport(server_cfg)(url, headers, _NOTIFICATION_PAYLOAD, timeout)


def _init_mcp_session(
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> Optional[str]:
    """
    Инициализировать MCP-сессию:
    - вызвать метод initialize;
    - прочитать mcp-session-id из заголовков ответа;
    - отправить notifications/initialized;
    - вернуть session_id.
    """
    headers = _mcp_headers(server_cfg, token, session_id=None)
    resp_headers, _body = get_transport(server_cfg)(
        url, headers, _initialize_payload(), timeout
    )
    session_id = _session_id_from(resp_headers)
    if not session_id:
        return None
    _send_initialized_notification(
        url, session_id, server_cfg, token=token, timeout=timeout
    )
    return session_id


async def _ainit_mcp_session(
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> Optional[str]:
    """Асинхронный двойник :func:`_init_mcp_session`; порядок шагов общий."""
    headers = _mcp_headers(server_cfg, token, session_id=None)
    resp_headers, _body = await aget_transport(server_cfg)(
        url, headers, _initialize_payload(), timeout
    )
    session_id = _session_id_from(resp_headers)
    if not session_id:
        return None
    await _asend_initialized_notification(
        url, session_id, server_cfg, token=token, timeout=timeout
    )
    return session_id


def _tools_list_request(
    url: str,
    session_id: str,
    server_cfg: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """Вызвать MCP-метод tools/list и вернуть список tools."""
    headers = _mcp_headers(server_cfg, token, session_id=session_id)
    _resp_headers, data = get_transport(server_cfg)(
        url, headers, _tools_list_payload(), timeout
    )
    return _tools_from(data)


async def _atools_list_request(
    url: str,
    session_id: str,
    server_cfg: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """Асинхронный двойник :func:`_tools_list_request`; разбор общий."""
    headers = _mcp_headers(server_cfg, token, session_id=session_id)
    _resp_headers, data = await aget_transport(server_cfg)(
        url, headers, _tools_list_payload(), timeout
    )
    return _tools_from(data)


def _remember_session(resp_headers: Dict[str, str], server_key: Optional[str]) -> None:
    """Сервер прислал новый mcp-session-id — запомнить его для этого владельца."""
    if not server_key:
        return
    new_sid = _session_id_from(resp_headers)
    if new_sid:
        _MCP_SESSIONS[(current_owner(), server_key)] = new_sid


def _call_mcp_tool(
    url: str,
    session_id: str,
    tool_name: str,
    arguments: Dict[str, Any],
    server_cfg: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 10.0,
    server_key: Optional[str] = None,
) -> Any:
    """
    Вызвать MCP-метод tools/call для конкретного инструмента.
    Если server_key передан и в ответе есть mcp-session-id — обновляем кэш сессии.
    """
    headers = _mcp_headers(server_cfg, token, session_id=session_id)
    resp_headers, body = get_transport(server_cfg)(
        url, headers, _tools_call_payload(tool_name, arguments), timeout
    )
    _remember_session(resp_headers, server_key)
    return body


async def _acall_mcp_tool(
    url: str,
    session_id: str,
    tool_name: str,
    arguments: Dict[str, Any],
    server_cfg: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 10.0,
    server_key: Optional[str] = None,
) -> Any:
    """Асинхронный двойник :func:`_call_mcp_tool`; обновление кэша сессии общее."""
    headers = _mcp_headers(server_cfg, token, session_id=session_id)
    resp_headers, body = await aget_transport(server_cfg)(
        url, headers, _tools_call_payload(tool_name, arguments), timeout
    )
    _remember_session(resp_headers, server_key)
    return body


def _get_or_init_session_id(
    server_key: str,
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> Optional[str]:
    """
    Вернуть session_id для MCP-сервера.
    Если в кэше нет — инициализировать сессию и сохранить.
    """
    session_id = _MCP_SESSIONS.get((current_owner(), server_key))
    if session_id:
        return session_id

    session_id = _init_mcp_session(
        url, server_cfg, token=token, timeout=timeout
    )
    if session_id:
        _MCP_SESSIONS[(current_owner(), server_key)] = session_id

    return session_id


async def _aget_or_init_session_id(
    server_key: str,
    url: str,
    server_cfg: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> Optional[str]:
    """Асинхронный двойник: кэш сессий общий, отличается только инициализация."""
    session_id = _MCP_SESSIONS.get((current_owner(), server_key))
    if session_id:
        return session_id

    session_id = await _ainit_mcp_session(
        url, server_cfg, token=token, timeout=timeout
    )
    if session_id:
        _MCP_SESSIONS[(current_owner(), server_key)] = session_id

    return session_id


__all__ = [
    "MCP_INIT_PARAMS",
    "_MCP_SESSIONS",
    "_normalize_url",
    "_send_initialized_notification",
    "_init_mcp_session",
    "_tools_list_request",
    "_atools_list_request",
    "_call_mcp_tool",
    "_acall_mcp_tool",
    "_get_or_init_session_id",
    "_aget_or_init_session_id",
]
