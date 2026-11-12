"""Код и текст причины отказа подключения MCP-сервера (спека §5.5).

Один источник причины для записи сервиса и для среза потребителя:
коды ``timeout``, ``connect_error``, ``auth``, ``command_not_found``,
``protocol``, ``other``; текст без значений секретов определения и с
обрезкой длинного сообщения.
"""

import httpx
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED

from gc_core.tools.connect_mcp.call_errors import (
    CALL_TIMEOUTS,
    McpServerGone,
    first_leaf,
)
from gc_core.tools.connect_mcp.client import McpProtocolError
from gc_core.tools.connect_mcp.durable_scope import McpSessionLostError
from gc_core.tools.connect_mcp.registry import _discovery_error_text
from gc_core.tools.connect_mcp.secret_mask import without_secrets
from gc_core.tools.connect_mcp.stream_client import is_connection_closed

# У подключения срок — любой: и срок вызова, и срок соединения.
_TIMEOUTS = (*CALL_TIMEOUTS, httpx.TimeoutException)
# Связь с сервером потеряна или не установлена. Потерянная сессия под
# durable — та же смерть сервера или срок постоянного stdio, но исход
# вызова неизвестен; для записи это обрыв связи.
_CONNECT_ERRORS = (
    OSError,
    httpx.TransportError,
    McpServerGone,
    McpSessionLostError,
)
_AUTH_STATUSES = frozenset({401, 403})
# Ошибки SDK ``mcp``, которые означают не ответ сервера, а срок или обрыв.
_MCP_ERROR_CODES = {
    httpx.codes.REQUEST_TIMEOUT: "timeout",
    # -32000 — ещё и начало диапазона серверных ошибок JSON-RPC: ответ
    # сервера с этим кодом тоже станет ``connect_error``.
    CONNECTION_CLOSED: "connect_error",
}


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, _TIMEOUTS):
        return "timeout"
    if isinstance(exc, FileNotFoundError):
        return "command_not_found"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return "auth" if status in _AUTH_STATUSES else "other"
    if isinstance(exc, McpProtocolError):
        return "protocol"
    if isinstance(exc, McpError):
        return _MCP_ERROR_CODES.get(exc.error.code, "other")
    if isinstance(exc, _CONNECT_ERRORS):
        return "connect_error"
    return "other"


def _failure_text(leaf: BaseException, server_cfg: dict, message: str) -> str:
    """Текст причины: свои формулировки сервиса, иначе — общие шаблоны
    обнаружения. Сырой текст httpx (адрес, ссылка на документацию) и
    текст SDK о разрыве в причину не идут."""
    if isinstance(leaf, httpx.HTTPStatusError):
        response = leaf.response
        status = f"{response.status_code} {response.reason_phrase}"
        return f"сервер ответил {status.strip()}"
    if isinstance(leaf, (httpx.ConnectTimeout, httpx.PoolTimeout)):
        target = server_cfg.get("url") or server_cfg.get("command") or "?"
        return f"нет соединения с {target}: истёк срок подключения"
    if is_connection_closed(leaf):
        return str(McpServerGone())
    return _discovery_error_text(leaf, server_cfg, message)


def classify_failure(exc: BaseException, server_cfg: dict) -> tuple[str, str]:
    """Код и текст причины отказа подключения.

    ``ExceptionGroup`` разворачивается до первого листа. Коды: ``timeout``,
    ``command_not_found``, ``auth``, ``protocol``, ``connect_error``,
    ``other``. Значения секретов определения в текст не попадают.
    """
    leaf = first_leaf(exc)
    # Сообщение маскируется до обрезки длинного текста: иначе секрет на
    # границе среза остался бы своим началом.
    # Итоговый текст маскируется ещё раз: шаблоны причины подставляют
    # ``command`` и ``url``, где тоже бывают секреты.
    message = without_secrets(str(leaf), server_cfg)
    reason = _failure_text(leaf, server_cfg, message)
    return _failure_code(leaf), without_secrets(reason, server_cfg)


__all__ = ["classify_failure"]
