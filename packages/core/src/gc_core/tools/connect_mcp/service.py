"""Сервис MCP хоста: общие записи серверов и каталог тулов.

Хост создаёт сервис и закрывает его; сервис не синглтон. Запись сервера —
пара «ключ, отпечаток» и общие для потребителей факты о нём: сырой список
тулов (из каталога или от последнего удачного подключения), время проверки
и последний отказ. Сам сервис в сеть не ходит и статусов не держит:
подключается потребитель, статус у каждого потребителя свой, а исход
подключения обновляет запись через ``record_success`` / ``record_failure``.

Сервис работает на одном цикле событий: он привязывается к циклу первого
обращения, обращение с другого цикла — ошибка. Закрывает сервис хост
(``aclose``): сервис перестаёт принимать работу и закрывает всех своих
потребителей в общий бюджет.
"""

import asyncio
import contextvars
import logging
import threading
import time
from functools import partial
from typing import Any, Awaitable, Callable

import httpx
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED

from gc_core.tools.connect_mcp.call_errors import (
    CALL_TIMEOUTS,
    McpServerGone,
    first_leaf,
)
from gc_core.tools.connect_mcp.catalog import read_catalog, write_catalog
from gc_core.tools.connect_mcp.client import McpProtocolError
from gc_core.tools.connect_mcp.consumer import McpConsumer
from gc_core.tools.connect_mcp.consumer_status import ServerStatus
from gc_core.tools.connect_mcp.durable_scope import McpSessionLostError
from gc_core.tools.connect_mcp.registry import (
    ToolsCache,
    _discovery_error_text,
)
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
from gc_core.tools.connect_mcp.stream_client import is_connection_closed

logger = logging.getLogger(__name__)

#: Отсрочка после срока бюджета закрытия сервиса: снятые сроком закрытия
#: потребителей ждутся не дольше неё. Снятие прерывает ожидание попыток,
#: вызовов и закрытий; хвост, начатый отменой (закрытие сессий во всех
#: реестрах, у постоянного stdio — до 7 с на сессию плюс close-тул), идёт
#: дальше без новых отмен, и что не уложилось в отсрочку, доигрывает в
#: фоне. Худший возврат ``aclose`` — бюджет плюс отсрочка, от числа
#: серверов и сессий не зависит.
_CLOSE_GRACE_SEC = 3.0

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


def _report_close(consumer: McpConsumer, task: asyncio.Task) -> None:
    """Исход закрытия потребителя — в лог; исключение задачи забирается."""
    if task.cancelled():
        outcome = "снято"
    elif task.exception() is not None:
        outcome = type(task.exception()).__name__
    else:
        return
    logger.warning(
        "Сервис MCP: потребитель %s закрылся не до конца: %s",
        consumer.label,
        outcome,
    )


class McpService:
    """Общие записи серверов хоста; политика подключения — для потребителей.

    ``_clock`` (вызываемое → float) и ``_sleep`` (асинхронное) — часы
    и сон сервиса и его потребителей; тесты подменяют их подставными.
    ``_stopped`` и ``_stopped_event`` ставит остановка сервиса. Событие
    создаётся сразу: ``asyncio.Event`` привязывается к циклу только при
    ожидании, поэтому остановка сервиса, ни разу не видевшего цикла,
    не падает. ``_consumers`` — открытые потребители; его, как и
    ``_stopped`` при выдаче потребителя, читают под ``_lock``: потребителя
    открывают с любого потока. ``_backend_cache`` — кэш тулов серверов,
    исполняемых внутри бэкенда сервиса (§5.10); заводится при первом
    запросе. ``_close_task`` — задача закрытия: её ждут все вызовы
    ``aclose``.
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
        self._backend_cache: ToolsCache | None = None
        self._close_task: asyncio.Task | None = None

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

        ``_loop`` пишут с цикла хоста (записи, сверка, ход, закрытие
        потребителя) без замка, а ``open_consumer`` и ``aclose`` сервиса —
        под ``_lock``: открыть потребителя могут и из другого потока.
        Поток без цикла сервис не привязывает, поэтому по контракту
        открытия (из потока без цикла или с цикла сервиса) привязывает
        сервис только цикл хоста.
        """
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("McpService работает на другом цикле событий")

    def open_consumer(self, label: str) -> McpConsumer:
        """Новый потребитель со своим владельцем сессий; с любого потока.

        После начала остановки сервиса — ``McpServiceClosed`` с любого
        потока и цикла. Иначе с цикла событий — обращение с цикла, как
        любое другое: сервис привязывается к нему, чужой цикл —
        ``RuntimeError`` до открытия (потребителя с чужого цикла не
        закрыть). Поток без цикла сервис не привязывает. Привязка идёт под
        замком: открывать потребителя могут разные потоки.
        """
        with self._lock:
            if self._stopped:
                raise McpServiceClosed(SERVICE_CLOSED_TEXT)
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                self._bind_loop()
            consumer = McpConsumer(self, label)
            self._consumers.add(consumer)
        return consumer

    def status(self, consumer: McpConsumer) -> dict[str, ServerStatus]:
        """Статусы серверов набора потребителя; с любого потока (§5.7).

        Набор и состояние потребителя по каждому серверу читаются под
        замком потребителя, под которым их меняет цикл.
        """
        return {
            key: consumer.status_of(entry)
            for key, entry in consumer.servers().items()
        }

    def backend_tools_cache(self) -> ToolsCache | None:
        """Кэш тулов серверов бэкенда сервиса; без бэкенда — ``None``.

        Звать с цикла сервиса. Кэш общий для потребителей: так его
        сегодня держит поверхность с бэкендом.
        """
        if self._os_backend is None:
            return None
        if self._backend_cache is None:
            self._backend_cache = ToolsCache(os_backend=self._os_backend)
        return self._backend_cache

    def _forget_consumer(self, consumer: McpConsumer) -> None:
        """Закрытый потребитель больше не числится за сервисом."""
        with self._lock:
            self._consumers.discard(consumer)

    async def aclose(self, budget: float = 10.0) -> None:
        """Закрыть сервис: отказать в работе и закрыть всех потребителей
        параллельно, в общий бюджет ``budget`` секунд (спека §5.12).

        Звать с цикла сервиса; с чужого цикла — ``RuntimeError`` до
        остановки. Непривязанный сервис закрывается с любого цикла.
        Остановка ставится сразу: ``open_consumer`` и ``aensure`` отвечают
        ``McpServiceClosed``, ``astart_turn`` и ``aend_turn`` ничего не
        делают, вызовы получают ошибку тула «сервис MCP остановлен»,
        ждущая подключений сверка просыпается.

        Первый вызов заводит задачу закрытия, и бюджет задаёт он;
        повторные и одновременные вызовы ждут ту же задачу, их бюджет не
        действует, после истёкшего бюджета повторный вызов возвращается
        сразу. Отмена вызывающего — его отмена, закрытие идёт дальше.
        Возврат — не позже ``budget + _CLOSE_GRACE_SEC``: по сроку бюджета
        предупреждение в лог (не исключение), незакрытые потребители
        снимаются, а их хвост закрытия сессий доигрывает в фоне;
        добивает процессы аварийное гашение. Задача закрытия живёт, пока
        живёт цикл сервиса: закрыть сервис надо до закрытия цикла.
        """
        with self._lock:
            self._bind_loop()
            self._stopped = True
            task = self._close_task
            if task is None:
                # Пустой контекст: стоп хода и владелец сессий вызывающего
                # в закрытие не попадают — владельца ставит потребитель.
                task = asyncio.get_running_loop().create_task(
                    self._aclose_consumers(budget),
                    context=contextvars.Context(),
                )
                self._close_task = task
        self._stopped_event.set()
        await asyncio.shield(task)

    async def _aclose_consumers(self, budget: float) -> None:
        """Закрыть потребителей, числящихся за сервисом, в общий бюджет.

        Остановка уже стоит под замком, поэтому снимок полон: нового
        потребителя после неё не открыть. По сроку — предупреждение,
        незакрытые снимаются один раз и ждутся не дольше
        ``_CLOSE_GRACE_SEC``. Исход каждого закрытия — в лог (у
        доигрывающих в фоне — по завершении); наружу исключения не идут.
        """
        with self._lock:
            consumers = list(self._consumers)
        if not consumers:
            return
        loop = asyncio.get_running_loop()
        tasks = {loop.create_task(c.aclose()): c for c in consumers}
        _, pending = await asyncio.wait(tasks, timeout=budget)
        if pending:
            logger.warning(
                "Сервис MCP: закрытие потребителей не уложилось в %s с",
                budget,
            )
            for task in pending:
                task.cancel()
            _, pending = await asyncio.wait(pending, timeout=_CLOSE_GRACE_SEC)
        for task, consumer in tasks.items():
            if task in pending:
                task.add_done_callback(partial(_report_close, consumer))
            else:
                _report_close(consumer, task)

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

    def record_interruption(self, entry: ServerEntry, started: float) -> None:
        """Попытку, начатую в ``started``, прервало закрытие её
        потребителя. Пишется всегда; правило 30 с учитывает отметку, только
        пока запись в отказе (``shared_hold_since``). Статус и отказ записи
        не меняются: это не отказ сервера."""
        self._bind_loop()
        if entry.interrupted_at is None or started > entry.interrupted_at:
            entry.interrupted_at = started

    def record_failure(
        self, entry: ServerEntry, exc: BaseException
    ) -> Failure:
        """Отказ подключения; тулы записи остаются."""
        self._bind_loop()
        failure = Failure(self._clock(), *classify_failure(exc, entry.cfg))
        entry.last_failure = failure
        return failure


__all__ = ["McpService", "classify_failure"]
