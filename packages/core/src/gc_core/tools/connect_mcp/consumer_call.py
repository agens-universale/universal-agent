"""Исполнитель вызова тула потребителя: чистые части (спека §5.4, §5.8).

Здесь — то, что не зависит от состояния потребителя: какой сервер и чья
сессия у вызова, ожидание своей попытки подключения под стопом хода и
перевод исходов вызова в ошибку тула. Состояние, замок и порядок шагов —
у потребителя (``McpConsumer._acall``).

Ошибка тула — ``McpToolCallError``: её не повторяет автоповтор, а модель
получает текст, а не сырое исключение транспорта.
"""

import asyncio

import httpx
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from gc_core.execution.tool_call_context import current_tool_call
from gc_core.tools.connect_mcp.call_errors import (
    McpCallTimedOut,
    McpServerGone,
)
from gc_core.tools.connect_mcp.server_identity import SESSION_KEY_FIELD
from gc_core.tools.connect_mcp.service_records import Failure
from gc_core.tools.connect_mcp.stream_client import McpToolCallError
from gc_core.user_stop import UserStopInterrupt, current_user_stop

#: Код JSON-RPC ошибки тула, которую отдаёт исполнитель.
TOOL_ERROR_CODE = -32000
#: Сервера с этим отпечатком нет в наборе потребителя.
SERVER_GONE_TEXT = "сервер отключён"
#: Сессию закрыл сам потребитель (отмена или сбой соседнего вызова).
SESSION_LOST_TEXT = "сессия сервера закрыта во время вызова"
#: Подключиться нельзя или не удалось; через двоеточие — причина.
UNAVAILABLE_TEXT = "сервер недоступен"

_TIMEOUTS = (McpCallTimedOut, TimeoutError, httpx.TimeoutException)
_TRANSPORT = (OSError, httpx.TransportError, McpError, McpServerGone)


def tool_error(message: str) -> McpToolCallError:
    """Ошибка тула с текстом для модели."""
    return McpToolCallError(ErrorData(code=TOOL_ERROR_CODE, message=message))


def call_target(
    full_tool_name: str,
    servers_cfg: dict[str, dict],
    tools_registry: dict[str, dict] | None,
) -> tuple[str | None, str | None]:
    """Ключ сервера тула и ключ сессии потребителя в его определении.

    Ключа сессии нет у сервера бэкенда и у неизвестного тула: такой вызов
    идёт прежним путём (там и разбор ошибок имени).
    """
    record = (tools_registry or {}).get(full_tool_name)
    meta = record.get("metadata") if isinstance(record, dict) else None
    key = meta.get("mcp_server_key") if isinstance(meta, dict) else None
    cfg = servers_cfg.get(key) if key else None
    session = cfg.get(SESSION_KEY_FIELD) if isinstance(cfg, dict) else None
    return key, session


def refusal_text(own: Failure | None, shared: Failure | None) -> str:
    """«Сервер недоступен» с причиной последнего отказа — своего или
    записи (при равном времени — своего)."""
    failures = [f for f in (own, shared) if f is not None]
    if not failures:
        return UNAVAILABLE_TEXT
    latest = max(failures, key=lambda failure: failure.at)
    return f"{UNAVAILABLE_TEXT}: {latest.reason}"


def is_timeout(exc: BaseException) -> bool:
    """Истёк срок вызова: сессия и статус остаются (§5.8 п.4)."""
    return isinstance(exc, _TIMEOUTS)


def timeout_error(exc: BaseException, timeout: float) -> McpToolCallError:
    """Ошибка тула по истёкшему сроку вызова."""
    if not isinstance(exc, McpCallTimedOut):
        exc = McpCallTimedOut(timeout)
    return tool_error(str(exc))


def is_transport_failure(exc: BaseException) -> bool:
    """Связь с сервером потеряна: ошибка соединения, смерть процесса,
    ошибка протокола. Не срок, не ошибка тула; группа — из таких же."""
    if isinstance(exc, BaseExceptionGroup):
        return all(is_transport_failure(e) for e in exc.exceptions)
    if isinstance(exc, (*_TIMEOUTS, McpToolCallError)):
        return False
    return isinstance(exc, _TRANSPORT)


async def await_attempt(
    waiting: asyncio.Future, *, timeout: float, server_key: str
) -> bool:
    """Исход своей попытки подключения — не дольше ``timeout``.

    ``waiting`` — отдельное ожидание из ``start_connect``: его отмена
    попытку не прерывает. Стоп хода снимает ожидание так же, как сам
    вызов (``UserStopInterrupt``), — подключение идёт дальше. Срок —
    ошибка тула.
    """
    stop = current_user_stop()
    if stop is None:
        return await _settle(waiting, timeout)
    if stop.requested:
        raise UserStopInterrupt(kind="mcp", started=False)
    tool, call_id = current_tool_call()

    def release() -> None:
        waiting.cancel()

    async with stop.arm(
        kind="mcp",
        tool=tool,
        call_id=call_id,
        mcp_server=server_key,
        release=release,
    ):
        return await _settle(waiting, timeout)


async def _settle(waiting: asyncio.Future, timeout: float) -> bool:
    done, _ = await asyncio.wait({waiting}, timeout=timeout)
    if not done:
        waiting.cancel()
        raise tool_error(
            f"{UNAVAILABLE_TEXT}: подключение не завершилось за {timeout:g} с"
        )
    if waiting.cancelled():  # снял стоп хода
        raise UserStopInterrupt(kind="mcp", started=False)
    return waiting.result()


__all__ = [
    "SERVER_GONE_TEXT",
    "SESSION_LOST_TEXT",
    "TOOL_ERROR_CODE",
    "UNAVAILABLE_TEXT",
    "await_attempt",
    "call_target",
    "is_timeout",
    "is_transport_failure",
    "refusal_text",
    "timeout_error",
    "tool_error",
]
