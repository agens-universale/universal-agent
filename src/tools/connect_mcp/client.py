from typing import Any, Dict, List, Optional

from tools.connect_mcp.transport import get_transport


# Параметры `initialize`, общие для всех локусов: один источник, иначе при подъёме
# версии протокола агентский и контейнерный путь разъедутся молча.
MCP_INIT_PARAMS: Dict[str, Any] = {
    "protocolVersion": "2024-11-05",
    "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
    "clientInfo": {"name": "SurfaceClient", "version": "1.0.0"},
}

# Кэш MCP-сессий в рамках процесса:
# ключ — имя сервера из конфига (server_name), значение — session_id.
_MCP_SESSIONS: Dict[str, str] = {}


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


def _send_initialized_notification(
    url: str,
    session_id: str,
    server_cfg: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> None:
    """
    Отправить notifications/initialized после успешного initialize.
    """
    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "mcp-session-id": session_id,
    }
    extra_headers: Dict[str, str] = server_cfg.get("headers") or {}
    headers.update(_merge_mcp_headers(token=token, extra_headers=extra_headers))

    request_fn = get_transport(server_cfg)
    _headers, _body = request_fn(url, headers, payload, timeout)


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
    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": MCP_INIT_PARAMS,
    }

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    extra_headers: Dict[str, str] = server_cfg.get("headers") or {}
    headers.update(_merge_mcp_headers(token=token, extra_headers=extra_headers))

    request_fn = get_transport(server_cfg)
    resp_headers, _body = request_fn(url, headers, payload, timeout)

    session_id = resp_headers.get("mcp-session-id") or resp_headers.get("MCP-Session-Id")
    if not session_id:
        return None

    _send_initialized_notification(
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
    """
    Вызвать MCP-метод tools/list и вернуть список tools.
    """
    headers: Dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "mcp-session-id": session_id,
    }
    extra_headers: Dict[str, str] = server_cfg.get("headers") or {}
    headers.update(_merge_mcp_headers(token=token, extra_headers=extra_headers))

    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": "tools-list-1",
        "method": "tools/list",
        "params": {},
    }

    request_fn = get_transport(server_cfg)
    _resp_headers, data = request_fn(url, headers, payload, timeout)

    # Ожидаемый формат JSON-RPC 2.0:
    # { "jsonrpc": "2.0", "id": "...", "result": { "tools": [...] } }
    if isinstance(data, dict):
        if isinstance(data.get("result"), dict):
            tools = data["result"].get("tools", [])
        else:
            tools = data.get("tools", [])
    else:
        tools = []

    if not isinstance(tools, list):
        return []

    return tools


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
    headers: Dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "mcp-session-id": session_id,
    }
    extra_headers: Dict[str, str] = server_cfg.get("headers") or {}
    headers.update(_merge_mcp_headers(token=token, extra_headers=extra_headers))

    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": "tools-call-1",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    request_fn = get_transport(server_cfg)
    resp_headers, body = request_fn(url, headers, payload, timeout)
    if server_key:
        new_sid = resp_headers.get("mcp-session-id") or resp_headers.get("MCP-Session-Id")
        if new_sid:
            _MCP_SESSIONS[server_key] = new_sid
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
    session_id = _MCP_SESSIONS.get(server_key)
    if session_id:
        return session_id

    session_id = _init_mcp_session(
        url, server_cfg, token=token, timeout=timeout
    )
    if session_id:
        _MCP_SESSIONS[server_key] = session_id

    return session_id


__all__ = [
    "MCP_INIT_PARAMS",
    "_MCP_SESSIONS",
    "_normalize_url",
    "_send_initialized_notification",
    "_init_mcp_session",
    "_tools_list_request",
    "_call_mcp_tool",
    "_get_or_init_session_id",
]
