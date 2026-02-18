from typing import Any, Dict, List, Optional

import requests

from config import config_dict as config_data


# Кэш MCP-сессий в рамках процесса:
# ключ — имя сервера из конфига (server_name), значение — session_id.
_MCP_SESSIONS: Dict[str, str] = {}


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
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> None:
    """
    Отправить notifications/initialized после успешного initialize.
    """
    url = _normalize_url(url)

    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "mcp-session-id": session_id,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()


def _init_mcp_session(
    url: str,
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
    url = _normalize_url(url)

    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "roots": {"listChanged": True},
                "sampling": {},
            },
            "clientInfo": {
                "name": "SurfaceClient",
                "version": "1.0.0",
            },
        },
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()

    # requests делает заголовки case-insensitive, так что нижний регистр сработает
    session_id = resp.headers.get("mcp-session-id") or resp.headers.get("MCP-Session-Id")
    if not session_id:
        return None

    _send_initialized_notification(url, session_id, token=token, timeout=timeout)

    return session_id


def _tools_list_request(
    url: str,
    session_id: str,
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """
    Вызвать MCP-метод tools/list по HTTP и вернуть список tools.
    """
    url = _normalize_url(url)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "mcp-session-id": session_id,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": "tools-list-1",
        "method": "tools/list",
        "params": {},
    }

    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()

    data = response.json()

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
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> Any:
    """
    Вызвать MCP-метод tools/call для конкретного инструмента.
    """
    url = _normalize_url(url)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "mcp-session-id": session_id,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": "tools-call-1",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()

    return response.json()


def _get_or_init_session_id(
    server_key: str,
    url: str,
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

    session_id = _init_mcp_session(url, token=token, timeout=timeout)
    if session_id:
        _MCP_SESSIONS[server_key] = session_id

    return session_id


__all__ = [
    "_MCP_SESSIONS",
    "_normalize_url",
    "_send_initialized_notification",
    "_init_mcp_session",
    "_tools_list_request",
    "_call_mcp_tool",
    "_get_or_init_session_id",
]
