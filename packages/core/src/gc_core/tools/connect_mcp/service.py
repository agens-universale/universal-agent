"""Сервис MCP хоста: общие записи серверов и каталог тулов.

Хост создаёт сервис и закрывает его; сервис не синглтон. Запись сервера —
пара «ключ, отпечаток» и общие для потребителей факты о нём: сырой список
тулов (из каталога или от последнего удачного подключения), время проверки
и последний отказ. Сам сервис в сеть не ходит и статусов не держит:
подключается потребитель, статус у каждого потребителя свой, а исход
подключения обновляет запись через ``record_success`` / ``record_failure``.

Сервис работает на одном цикле событий: он привязывается к циклу первого
обращения, обращение с другого цикла — ошибка.
"""

import asyncio
import threading
import time
from typing import Any, Awaitable, Callable

import httpx
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED

from gc_core.tools.connect_mcp.call_errors import (
    McpCallTimedOut,
    McpServerGone,
)
from gc_core.tools.connect_mcp.catalog import read_catalog, write_catalog
from gc_core.tools.connect_mcp.client import McpProtocolError
from gc_core.tools.connect_mcp.consumer import McpConsumer
from gc_core.tools.connect_mcp.durable_scope import McpSessionLostError
from gc_core.tools.connect_mcp.registry import _discovery_error_text
from gc_core.tools.connect_mcp.secret_mask import without_secrets
from gc_core.tools.connect_mcp.server_identity import (
    connection_fingerprint,
    with_service_defaults,
)
from gc_core.tools.connect_mcp.service_records import (
    SERVICE_CLOSED_TEXT,
    Failure,
    McpServiceClosed,
    ServerEntry,
)

_TIMEOUTS = (TimeoutError, McpCallTimedOut, httpx.TimeoutException)
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


def _first_leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


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


def classify_failure(exc: BaseException, server_cfg: dict) -> tuple[str, str]:
    """Код и текст причины отказа подключения.

    ``ExceptionGroup`` разворачивается до первого листа. Коды: ``timeout``,
    ``command_not_found``, ``auth``, ``protocol``, ``connect_error``,
    ``other``. Значения секретов определения в текст не попадают.
    """
    leaf = _first_leaf(exc)
    # Сообщение маскируется до обрезки длинного текста: иначе секрет на
    # границе среза остался бы своим началом.
    # Итоговый текст маскируется ещё раз: шаблоны причины подставляют
    # ``command`` и ``url``, где тоже бывают секреты.
    message = without_secrets(str(leaf), server_cfg)
    reason = _discovery_error_text(leaf, server_cfg, message)
    return _failure_code(leaf), without_secrets(reason, server_cfg)


class McpService:
    """Общие записи серверов хоста; политика подключения — для потребителей.

    ``_clock`` (вызываемое → float) и ``_sleep`` (асинхронное) — часы
    и сон сервиса и его потребителей; тесты подменяют их подставными.
    ``_stopped`` и ``_stopped_event`` ставит остановка сервиса. Событие
    создаётся сразу: ``asyncio.Event`` привязывается к циклу только при
    ожидании, поэтому остановка сервиса, ни разу не видевшего цикла,
    не падает. ``_consumers`` — открытые потребители; его, как и
    ``_stopped`` при выдаче потребителя, читают под ``_lock``: потребителя
    открывают с любого потока.
    """

    def __init__(
        self,
        *,
        connect_on_start: bool,
        wait_all: bool = False,
        os_backend: Any = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._connect_on_start = connect_on_start
        self._wait_all = wait_all
        self._os_backend = os_backend
        self._clock = clock
        self._sleep = sleep
        self._entries: dict[tuple[str, str], ServerEntry] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped = False
        self._stopped_event = asyncio.Event()
        self._lock = threading.Lock()
        self._consumers: set[McpConsumer] = set()

    @property
    def os_backend(self) -> Any:
        return self._os_backend

    @property
    def connect_on_start(self) -> bool:
        return self._connect_on_start

    @property
    def wait_all(self) -> bool:
        return self._wait_all

    @property
    def closed(self) -> bool:
        return self._stopped

    def _bind_loop(self) -> None:
        """Привязать сервис к текущему циклу; чужой цикл — ``RuntimeError``.

        Проверка и установка цикла идут без замка: они рассчитаны на
        обращения с одного потока — потока цикла хоста.
        """
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("McpService работает на другом цикле событий")

    def open_consumer(self, label: str) -> McpConsumer:
        """Новый потребитель со своим владельцем сессий; с любого потока.

        После начала остановки сервиса — ``McpServiceClosed``.
        """
        with self._lock:
            if self._stopped:
                raise McpServiceClosed(SERVICE_CLOSED_TEXT)
            consumer = McpConsumer(self, label)
            self._consumers.add(consumer)
        return consumer

    def _forget_consumer(self, consumer: McpConsumer) -> None:
        """Закрытый потребитель больше не числится за сервисом."""
        with self._lock:
            self._consumers.discard(consumer)

    def entry(
        self, server_key: str, server_cfg: dict, *, discovery_timeout: float
    ) -> ServerEntry:
        """Запись сервера по ``(ключ, отпечаток)``; новая берёт тулы из
        каталога, если он есть. ``server_cfg`` — определение без trace."""
        self._bind_loop()
        fingerprint = connection_fingerprint(server_cfg)
        found = self._entries.get((server_key, fingerprint))
        if found is not None:
            found.discovery_timeout = discovery_timeout
            return found
        cfg = with_service_defaults(server_cfg)
        created = ServerEntry(
            key=server_key,
            cfg=cfg,
            fingerprint=fingerprint,
            discovery_timeout=discovery_timeout,
        )
        cataloged = read_catalog(server_key, cfg)
        if cataloged is not None:
            created.tools = tuple(cataloged)
            created.from_catalog = True
        self._entries[(server_key, fingerprint)] = created
        return created

    def record_success(self, entry: ServerEntry, tools: list[dict]) -> None:
        """Удачное подключение: список тулов и время проверки; каталог
        перезаписывается, только если список стал известен или изменился."""
        self._bind_loop()
        listed = tuple(tools)
        if not entry.tools_known or listed != entry.tools:
            write_catalog(entry.key, entry.cfg, list(listed))
        entry.tools = listed
        entry.checked_at = self._clock()
        entry.from_catalog = False

    def record_failure(
        self, entry: ServerEntry, exc: BaseException
    ) -> Failure:
        """Отказ подключения; тулы записи остаются."""
        self._bind_loop()
        failure = Failure(self._clock(), *classify_failure(exc, entry.cfg))
        entry.last_failure = failure
        return failure


__all__ = ["McpService", "classify_failure"]
