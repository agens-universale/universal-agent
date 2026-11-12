"""Исполнитель вызова тула потребителя: чистые части (спека §5.4, §5.8).

Здесь — то, что не зависит от состояния потребителя: какой сервер и чья
сессия у вызова, ожидание своей попытки подключения под стопом хода и
перевод исходов вызова в ошибку тула. Состояние и замок — у
``McpConnections``, порядок шагов — у ``McpCallExecutor.acall``.

Ошибка тула — ``McpToolCallError``: её не повторяет автоповтор, а модель
получает текст, а не сырое исключение транспорта.

Классификация исхода — по первому листу группы: перечислено то, что
уходит как есть, и срок; всё остальное — отказ связи с сервером.
"""

import asyncio

import anyio
import httpx
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from gc_core.execution.contracts import ToolOutcomeUnknown
from gc_core.execution.tool_call_context import current_tool_call
from gc_core.tools.connect_mcp.call_errors import (
    CALL_TIMEOUTS,
    McpCallOutcomeUnknown,
    McpCallTimedOut,
)
from gc_core.tools.connect_mcp.server_identity import SESSION_KEY_FIELD
from gc_core.tools.connect_mcp.service_records import Failure
from gc_core.tools.connect_mcp.stream_client import (
    McpToolCallError,
    is_connection_closed,
)
from gc_core.user_stop import UserStopInterrupt, current_user_stop

#: Код JSON-RPC ошибки тула, которую отдаёт исполнитель.
TOOL_ERROR_CODE = -32000
#: Сервера с этим отпечатком нет в наборе потребителя.
SERVER_GONE_TEXT = "сервер отключён"
#: Сессию закрыл сам потребитель (отмена или сбой соседнего вызова).
SESSION_LOST_TEXT = "сессия сервера закрыта во время вызова"
#: Подключиться нельзя или не удалось; через двоеточие — причина.
UNAVAILABLE_TEXT = "сервер недоступен"

# Уходят к вызывающему как есть: ошибка разбора имени и аргументов
# (``ValueError``, в том числе ``pydantic.ValidationError`` и
# ``McpExecuteOnError``), стоп хода, «исход неизвестен» durable
# (``ToolOutcomeUnknown`` и ``McpSessionLostError``). Ошибка тула сервера —
# тоже, если это не разрыв соединения (``passes_through``).
_PASS_THROUGH = (ValueError, UserStopInterrupt, ToolOutcomeUnknown)
# Не исход вызова, а конец процесса: уходят как есть и листом группы.
_FATAL = (KeyboardInterrupt, SystemExit)
# Семейства, которыми транспорты сообщают об отказе связи. Лист вне них —
# скорее ошибка нашего кода: в лог он идёт ошибкой с кадрами трассировки.
# ``RuntimeError`` — семейство транспорта: им отвечают постоянный stdio
# («process is not running») и легаси-http («Failed to initialize …»).
# Явные ошибки кода из его поддерева — ``_CODE_ERRORS`` — в семейство не
# входят.
_TRANSPORT_FAMILIES = (
    OSError,
    RuntimeError,
    httpx.HTTPError,
    McpError,
    McpCallOutcomeUnknown,
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
)
_CODE_ERRORS = (NotImplementedError, RecursionError)


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


def is_timeout(leaf: BaseException) -> bool:
    """Истёк срок вызова: сессия и статус остаются (§5.8 п.4)."""
    return isinstance(leaf, CALL_TIMEOUTS)


def timeout_error(leaf: BaseException, timeout: float) -> McpToolCallError:
    """Ошибка тула по истёкшему сроку вызова."""
    if not isinstance(leaf, McpCallTimedOut):
        leaf = McpCallTimedOut(timeout)
    return tool_error(str(leaf))


def passes_through(leaf: BaseException) -> bool:
    """Исход уходит к вызывающему как есть: ответ сервера ошибкой тула
    (кроме разрыва соединения), ошибка аргументов, стоп, durable, конец
    процесса (``KeyboardInterrupt``, ``SystemExit``)."""
    if isinstance(leaf, McpToolCallError):
        return not is_connection_closed(leaf)
    return isinstance(leaf, (*_PASS_THROUGH, *_FATAL))


def is_transport_family(leaf: BaseException) -> bool:
    """Лист из семейств, которыми транспорты сообщают об отказе связи."""
    if isinstance(leaf, _CODE_ERRORS):
        return False
    return isinstance(leaf, _TRANSPORT_FAMILIES)


def stop_requested() -> bool:
    """Пользователь запросил стоп текущего хода."""
    stop = current_user_stop()
    return stop is not None and stop.requested


def lost_session() -> ConnectionError:
    """Вызов снят транспортом (отмена не закрытием потребителя)."""
    return ConnectionError(SESSION_LOST_TEXT)


async def await_attempt(
    waiting: asyncio.Future,
    *,
    deadline: float,
    timeout: float,
    server_key: str,
) -> bool:
    """Исход своей попытки подключения — не позже ``deadline`` (время
    цикла); ``timeout`` — полный потолок, для текста ошибки.

    ``waiting`` — отдельное ожидание из ``start_connect``: его отмена
    попытку не прерывает. Стоп хода снимает ожидание так же, как сам
    вызов (``UserStopInterrupt``), — подключение идёт дальше. Срок —
    ошибка тула.
    """
    stop = current_user_stop()
    if stop is None:
        return await _settle(waiting, deadline, timeout)
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
        return await _settle(waiting, deadline, timeout)


async def _settle(
    waiting: asyncio.Future, deadline: float, timeout: float
) -> bool:
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    done, _ = await asyncio.wait({waiting}, timeout=remaining)
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
    "is_transport_family",
    "lost_session",
    "passes_through",
    "refusal_text",
    "stop_requested",
    "timeout_error",
    "tool_error",
]
