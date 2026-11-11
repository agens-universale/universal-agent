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
import time
from dataclasses import dataclass
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
from gc_core.tools.connect_mcp.durable_scope import McpSessionLostError
from gc_core.tools.connect_mcp.registry import _humanize_discovery_error
from gc_core.tools.connect_mcp.server_identity import (
    connection_fingerprint,
    with_service_defaults,
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
    CONNECTION_CLOSED: "connect_error",
}
# Короче — не маскируем: в ``env`` бывают «1» и «true», замена их
# в тексте причины испортила бы числа и адреса.
_MIN_SECRET_LEN = 8
_SECRET_MASK = "***"


class McpServiceClosed(RuntimeError):
    """Обращение к сервису после начала его остановки."""


@dataclass(frozen=True)
class Failure:
    """Отказ подключения: когда (часы сервиса), код и текст причины."""

    at: float
    code: str
    reason: str


@dataclass(eq=False)
class ServerEntry:
    """Общие факты о сервере для всех потребителей сервиса.

    ``cfg`` — определение без trace (``plain_dump(model, by_alias=True)``)
    после ``with_service_defaults``; ``tools`` — сырой ответ
    ``tools/list``, до whitelist и оформления.
    """

    key: str
    cfg: dict
    fingerprint: str
    discovery_timeout: float
    tools: tuple = ()
    from_catalog: bool = False
    checked_at: float | None = None
    last_failure: Failure | None = None


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


def _secret_values(server_cfg: dict) -> list[str]:
    values = [server_cfg.get("token")]
    for field in ("headers", "env"):
        values.extend((server_cfg.get(field) or {}).values())
    return [
        v for v in values if isinstance(v, str) and len(v) >= _MIN_SECRET_LEN
    ]


def _without_secrets(text: str, server_cfg: dict) -> str:
    for secret in sorted(_secret_values(server_cfg), key=len, reverse=True):
        text = text.replace(secret, _SECRET_MASK)
    return text


def classify_failure(exc: BaseException, server_cfg: dict) -> tuple[str, str]:
    """Код и текст причины отказа подключения.

    ``ExceptionGroup`` разворачивается до первого листа. Коды: ``timeout``,
    ``command_not_found``, ``auth``, ``protocol``, ``connect_error``,
    ``other``. Значения секретов определения в текст не попадают.
    """
    leaf = _first_leaf(exc)
    reason = _humanize_discovery_error(leaf, server_cfg)
    return _failure_code(leaf), _without_secrets(reason, server_cfg)


class McpService:
    """Общие записи серверов хоста; политика подключения — для потребителей.

    ``_clock`` (вызываемое → float) и ``_sleep`` (асинхронное) — часы
    и сон сервиса и его потребителей; тесты подменяют их подставными.
    ``_stopped`` и ``_stopped_event`` ставит остановка сервиса. Событие
    создаётся сразу: ``asyncio.Event`` привязывается к циклу только при
    ожидании, поэтому остановка сервиса, ни разу не видевшего цикла,
    не падает.
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
        """Привязать сервис к текущему циклу; чужой цикл — ``RuntimeError``."""
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("McpService работает на другом цикле событий")

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
        known = entry.from_catalog or entry.checked_at is not None
        if not known or listed != entry.tools:
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


__all__ = [
    "Failure",
    "McpService",
    "McpServiceClosed",
    "ServerEntry",
    "classify_failure",
]
