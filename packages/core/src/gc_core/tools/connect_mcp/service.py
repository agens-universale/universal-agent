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
import re
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
from gc_core.tools.connect_mcp.registry import _discovery_error_text
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
    # -32000 — ещё и начало диапазона серверных ошибок JSON-RPC: ответ
    # сервера с этим кодом тоже станет ``connect_error``.
    CONNECTION_CLOSED: "connect_error",
}
# Пороги маскировки секретов — см. ``_without_secrets``.
_MIN_SECRET_LEN = 8
_MIN_WORD_SECRET_LEN = 4
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


def _secret_fragments(server_cfg: dict) -> set[str]:
    """Значения ``token``, ``headers`` и ``env`` целиком и их части между
    пробелами (``Bearer <ключ>`` — ещё и ``<ключ>``)."""
    values = [server_cfg.get("token")]
    for field in ("headers", "env"):
        values.extend((server_cfg.get(field) or {}).values())
    fragments: set[str] = set()
    for value in values:
        if isinstance(value, str):
            fragments.add(value)
            fragments.update(value.split())
    return {f for f in fragments if len(f) >= _MIN_WORD_SECRET_LEN}


def _mask_fragment(text: str, fragment: str) -> str:
    if len(fragment) >= _MIN_SECRET_LEN:
        return text.replace(fragment, _SECRET_MASK)
    word = rf"(?<!\w){re.escape(fragment)}(?!\w)"
    return re.sub(word, _SECRET_MASK, text)


def _without_secrets(text: str, server_cfg: dict) -> str:
    """Текст без значений секретов определения.

    Фрагмент от 8 символов заменяется везде, где встретился; от 4 до 7 —
    только отдельным словом (слева и справа не буква, не цифра и не
    подчёркивание), чтобы не портить числа и адреса. Длинные фрагменты
    идут первыми.

    Не покрыто: фрагменты короче 4 символов и значение из 4–7 символов
    внутри слова; секрет в экранированной форме (``repr`` с ``\\`` или
    ``'``, JSON- и URL-кодирование) и в другом регистре. Побочный эффект
    порогов: часть значения из 4–7 символов, стоящая отдельным словом,
    маскируется, даже если она не секрет (порт ``8001``, ``Bearer``).
    """
    for fragment in sorted(
        _secret_fragments(server_cfg), key=len, reverse=True
    ):
        text = _mask_fragment(text, fragment)
    return text


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
    message = _without_secrets(str(leaf), server_cfg)
    reason = _discovery_error_text(leaf, server_cfg, message)
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
        """Привязать сервис к текущему циклу; чужой цикл — ``RuntimeError``.

        Проверка и установка цикла идут без замка: они рассчитаны на
        обращения с одного потока — потока цикла хоста.
        """
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
