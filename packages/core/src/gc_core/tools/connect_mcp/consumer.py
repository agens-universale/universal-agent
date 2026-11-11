"""Потребитель сервиса MCP: подключение своей сессией и свой статус.

Потребитель — то, чьи это сессии (окно TUI, агент ACP, прогон ``gc-cli``).
Подключить сервер — открыть его сессию под владельцем потребителя:
``initialize`` и ``tools/list`` по тому же определению и ключу сессии, что
получат обёртки тулов; эта же сессия потом обслуживает вызовы. Исход
подключения обновляет общую запись сервиса, а статус сервера у каждого
потребителя свой: собственная сессия, собственная попытка, собственный
отказ.

Состояние меняется только на цикле сервиса и под ``_lock``: статус читают
и с других потоков. Фоновая работа (попытки, серии повторов, закрытие
сессий) идёт задачами с пустым контекстом переменных — пользовательский
стоп хода и чужой владелец туда не попадают; владельца и слоты потребитель
ставит явно. В stdout она не пишет: только лог и статус.
"""

import asyncio
import contextvars
import dataclasses
import logging
import threading
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from gc_core.mcp_session_owner import new_owner_token, use_owner
from gc_core.settings import settings_of
from gc_core.tools.connect_mcp import stream_client
from gc_core.tools.connect_mcp.abandonable import use_call_slots
from gc_core.tools.connect_mcp.agent_tools import mcp_cache_config
from gc_core.tools.connect_mcp.client import (
    forget_owner_sessions,
    forget_session,
)
from gc_core.tools.connect_mcp.persistent_stdio import (
    PersistentStdioRegistry,
)
from gc_core.tools.connect_mcp.registry import alist_raw_tools
from gc_core.tools.connect_mcp.secret_mask import without_secrets
from gc_core.tools.connect_mcp.server_identity import (
    SESSION_KEY_FIELD,
    is_process_transport,
    session_key,
    with_service_defaults,
)
from gc_core.tools.connect_mcp.service_records import (
    SERVICE_CLOSED_TEXT,
    Failure,
    McpServiceClosed,
    ServerEntry,
)

if TYPE_CHECKING:
    from gc_core.tools.connect_mcp.service import McpService

logger = logging.getLogger(__name__)

#: Паузы фоновых повторов сетевого сервера у потребителя «на старте».
RETRY_PAUSES_SEC = (1, 3, 10, 30, 60)
#: Правило 30 с: не раньше этого после последнего отказа — новая попытка.
ATTEMPT_INTERVAL_SEC = 30
STATE_CACHED = "cached"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_FAILED = "failed"

_SLOTS_PER_CONSUMER = 8
#: Внутренний срок списка длиннее внешнего срока подключения: истёкшее
#: время подключения приходит как ``TimeoutError`` внешнего ожидания.
_LIST_SLACK_SEC = 5
_INTERRUPTED = "подключение прервано"
_INTERNAL_ERROR = "внутренняя ошибка подключения"


@dataclass(frozen=True)
class ServerStatus:
    """Статус сервера у потребителя (спека §5.5).

    ``from_catalog`` — тулы взяты из записи, а не из своей сессии
    (``connecting`` с известным списком и ``cached``); ``retry`` — номер
    идущего повтора серии; ``tools`` — сырой список записи, у ``failed``
    пуст.
    """

    state: str
    reason_code: str = ""
    reason: str = ""
    retry: int = 0
    from_catalog: bool = False
    checked_at: float | None = None
    tools: tuple = ()


def _session_key_of(entry: ServerEntry) -> str:
    return session_key(entry.key, entry.fingerprint)


def _is_interrupted(failure: Failure) -> bool:
    return failure.code == "other" and failure.reason == _INTERRUPTED


def _shared_failure(entry: ServerEntry) -> Failure | None:
    """Последний отказ записи, если он позже её последней удачи.

    Удача другого потребителя после отказа значит, что сервер уже
    поднимается: такой отказ правило 30 с не держит.
    """
    failure = entry.last_failure
    if failure is None:
        return None
    if entry.checked_at is not None and failure.at <= entry.checked_at:
        return None
    return failure


def _describe(exc: BaseException, entry: ServerEntry) -> str:
    """Тип и текст исключения для лога — без значений секретов.

    Трейсбэк в лог не идёт: в цепочке исключений лежит исходная ошибка
    подключения, а тексты ошибок httpx несут адрес со строкой запроса.
    """
    return f"{type(exc).__name__}: {without_secrets(str(exc), entry.cfg)}"


def _succeeded(task: asyncio.Task) -> bool:
    """Попытка завершилась удачей (не отменена, без исключения, истина)."""
    return not task.cancelled() and task.exception() is None and task.result()


def _failed(failure: Failure, entry: ServerEntry, retry: int = 0):
    return ServerStatus(
        STATE_FAILED,
        reason_code=failure.code,
        reason=failure.reason,
        retry=retry,
        checked_at=entry.checked_at,
    )


class McpConsumer:
    """Сессии одного потребителя сервиса и его статусы серверов.

    Состояние по серверам ключуется ключом сессии (ключ сервера и
    отпечаток): ``_sessions`` — постоянная сессия есть; ``_ok`` — удача у
    сервера без постоянной сессии; ``_connects`` — идущая попытка;
    ``_series`` — серия повторов; ``_failures`` — своя последняя неудачная
    или прерванная попытка; ``_retry`` — номер повтора серии; ``_closing`` —
    закрытие остатков сессий после отказа или отмены попытки.
    ``_settings`` — настройки сборки, по которым строятся определения.
    """

    def __init__(self, service: "McpService", label: str) -> None:
        self._service = service
        self.owner = new_owner_token("mcp")
        self.label = label
        self.closed = False
        self._slots = threading.BoundedSemaphore(_SLOTS_PER_CONSUMER)
        self._settings: Any = None
        self._lock = threading.Lock()
        self._sessions: set[str] = set()
        self._ok: set[str] = set()
        self._connects: dict[str, asyncio.Task] = {}
        self._series: dict[str, asyncio.Task] = {}
        self._failures: dict[str, Failure] = {}
        self._retry: dict[str, int] = {}
        self._closing: set[asyncio.Task] = set()
        # Хвост закрытия (сессии владельца) идёт по одному вызову за раз.
        self._closing_sessions = asyncio.Lock()

    def wrapper_cfg(self, entry: ServerEntry) -> dict:
        """Определение для подключения и обёрток тулов.

        Из настроек сборки (алиасы и trace этой сборки), с умолчаниями
        сервиса и ключом сессии потребителя.
        """
        servers = mcp_cache_config(self._settings)["mcp-servers"]
        cfg = with_service_defaults(servers[entry.key])
        cfg[SESSION_KEY_FIELD] = _session_key_of(entry)
        return cfg

    def has_session(self, entry: ServerEntry) -> bool:
        with self._lock:
            return _session_key_of(entry) in self._sessions

    def _connected(self, key: str) -> bool:
        """Сессия есть или удача без постоянной сессии. Звать под
        ``_lock``."""
        return key in self._sessions or key in self._ok

    def _attempting(self, key: str) -> bool:
        """Идёт своя попытка. Звать под ``_lock``."""
        task = self._connects.get(key)
        return task is not None and not task.done()

    def may_connect(self, entry: ServerEntry) -> bool:
        """Правило 30 с: своей попытки нет, с последнего отказа — 30 с.

        Последний отказ — более позднее из своей неудачной или прерванной
        попытки и отказа записи (если он позже удачи записи).
        """
        key = _session_key_of(entry)
        with self._lock:
            if self._attempting(key):
                return False
            own = self._failures.get(key)
        failures = (own, _shared_failure(entry))
        since = [f.at for f in failures if f is not None]
        if not since:
            return True
        elapsed = self._service._clock() - max(since)
        return elapsed >= ATTEMPT_INTERVAL_SEC

    def start_connect(self, entry: ServerEntry) -> asyncio.Future:
        """Одна попытка подключения; идущая попытка не дублируется.

        Попытка числится за потребителем сразу, до первого шага задачи:
        статус — ``connecting`` с этого момента. Отдаётся отдельное
        ожидание исхода попытки: его отмена (в том числе срок
        ``wait_for``) общую попытку не прерывает. Исход — истина при
        удаче, ложь при отказе, прерывании и внутренней ошибке; причина —
        в статусе. Саму задачу держит ``_connects``: её
        отменяют только сверка и закрытие потребителя. Отмена до первого
        шага живую сессию не трогает — ключ остаётся в ``_sessions``
        или ``_ok``.
        """
        task = self._start_attempt(entry)
        waiting = asyncio.get_running_loop().create_future()

        def settle(done: asyncio.Task) -> None:
            if not waiting.done():
                waiting.set_result(_succeeded(done))

        task.add_done_callback(settle)
        return waiting

    def _start_attempt(self, entry: ServerEntry) -> asyncio.Task:
        """Задача попытки (идущая или новая); после ``aclose`` —
        ``McpServiceClosed``."""
        self._service._bind_loop()
        key = _session_key_of(entry)
        with self._lock:
            if self.closed:
                raise McpServiceClosed(SERVICE_CLOSED_TEXT)
            running = self._connects.get(key)
            if running is not None and not running.done():
                return running
            started = self._service._clock()
            task = asyncio.get_running_loop().create_task(
                self._aconnect(entry, started),
                context=contextvars.Context(),
            )
            self._connects[key] = task
        task.add_done_callback(partial(self._on_connect_done, entry, started))
        return task

    async def _aconnect(self, entry: ServerEntry, started: float) -> bool:
        key = _session_key_of(entry)
        with use_owner(self.owner), use_call_slots(self._slots):
            try:
                await self._alist_and_record(entry)
                return True
            except asyncio.CancelledError:
                self._drop_connection(key)
                await self._aclose_rest(entry)
                raise
            except Exception as exc:
                # Сначала снять сессию и закрыть остаток: если запись
                # отказа сама бросит, ключ не останется «подключённым».
                self._drop_connection(key)
                await self._aclose_rest(entry)
                self._record_failure(entry, exc, started)
                return False

    async def _alist_and_record(self, entry: ServerEntry) -> None:
        cfg = self.wrapper_cfg(entry)
        timeout = settings_of(self._settings).mcp.meta.discovery_timeout_sec
        raw = await asyncio.wait_for(
            alist_raw_tools(entry.key, cfg, timeout=timeout + _LIST_SLACK_SEC),
            timeout,
        )
        self._service.record_success(entry, raw)
        key = _session_key_of(entry)
        with self._lock:
            self._failures.pop(key, None)
            self._retry.pop(key, None)
            target = self._sessions if cfg["persistent_session"] else self._ok
            target.add(key)

    def _record_failure(
        self, entry: ServerEntry, exc: Exception, started: float
    ) -> None:
        """Отказ — в запись; свой отказ считается от начала попытки."""
        failure = self._service.record_failure(entry, exc)
        logger.warning(
            "MCP %s: подключение потребителя %s не удалось: %s",
            entry.key,
            self.label,
            failure.reason,
        )
        with self._lock:
            self._failures[_session_key_of(entry)] = dataclasses.replace(
                failure, at=started
            )

    def _drop_connection(self, key: str) -> None:
        with self._lock:
            self._sessions.discard(key)
            self._ok.discard(key)

    async def _aclose_rest(self, entry: ServerEntry) -> None:
        """Закрыть остаток сессии; повторная отмена закрытие не обрывает."""
        task = asyncio.get_running_loop().create_task(
            self._aclose_session(entry), context=contextvars.Context()
        )
        with self._lock:
            self._closing.add(task)
        task.add_done_callback(self._forget_closing)
        await asyncio.shield(task)

    def _forget_closing(self, task: asyncio.Task) -> None:
        with self._lock:
            self._closing.discard(task)

    async def _aclose_session(self, entry: ServerEntry) -> None:
        """Сессия сервера у потребителя — во всех трёх реестрах."""
        key = _session_key_of(entry)
        registry = PersistentStdioRegistry.instance()
        manager = stream_client._SESSION_MANAGER
        with use_owner(self.owner):
            for close in (registry.aclose_one, manager.ainvalidate):
                try:
                    await close(key)
                except Exception as exc:
                    logger.warning(
                        "MCP %s: сессия потребителя %s не закрылась: %s",
                        entry.key,
                        self.label,
                        _describe(exc, entry),
                    )
            forget_session(entry.key)

    def _on_connect_done(
        self, entry: ServerEntry, started: float, task: asyncio.Task
    ) -> None:
        key = _session_key_of(entry)
        with self._lock:
            if self._connects.get(key) is task:
                del self._connects[key]
            if task.cancelled():
                self._failures[key] = Failure(started, "other", _INTERRUPTED)
                return
        if self._unexpected_failure(entry, task):
            return
        if task.result() is False and self._retries(entry):
            self._start_series(entry)

    def _unexpected_failure(
        self, entry: ServerEntry, task: asyncio.Task
    ) -> bool:
        """Исключение фоновой задачи — отказ ``other``, не тишина."""
        exc = task.exception()
        if exc is None:
            return False
        logger.error(
            "MCP %s: фоновая работа потребителя %s упала: %s",
            entry.key,
            self.label,
            _describe(exc, entry),
        )
        failure = Failure(self._service._clock(), "other", _INTERNAL_ERROR)
        with self._lock:
            self._failures[_session_key_of(entry)] = failure
        return True

    def _retries(self, entry: ServerEntry) -> bool:
        """Повторы — у сетевого сервера и потребителя «на старте»."""
        return self._service.connect_on_start and not is_process_transport(
            entry.cfg
        )

    def _start_series(self, entry: ServerEntry) -> None:
        key = _session_key_of(entry)
        with self._lock:
            if self.closed or key in self._series:
                return
            task = asyncio.get_running_loop().create_task(
                self._aretry(entry), context=contextvars.Context()
            )
            self._series[key] = task
        task.add_done_callback(partial(self._on_series_done, entry))

    async def _aretry(self, entry: ServerEntry) -> None:
        """Серия повторов: пауза, одна попытка, удача — конец серии."""
        key = _session_key_of(entry)
        for attempt, pause in enumerate(RETRY_PAUSES_SEC, start=1):
            with self._lock:
                if self.closed:
                    return
                self._retry[key] = attempt
            await self._service._sleep(pause)
            with self._lock:
                # За паузу сервер мог подключиться (вызов, вход в ход):
                # попытка поверх живой сессии её бы и закрыла при отказе.
                if self.closed or self._connected(key):
                    return
            # Ждём без отмены попытки: снятие серии её не прерывает.
            task = self._start_attempt(entry)
            await asyncio.wait({task})
            if _succeeded(task):
                return

    def _on_series_done(self, entry: ServerEntry, task: asyncio.Task) -> None:
        key = _session_key_of(entry)
        with self._lock:
            self._retry.pop(key, None)
            if self._series.get(key) is task:
                del self._series[key]
        if not task.cancelled():
            self._unexpected_failure(entry, task)

    def status_of(self, entry: ServerEntry) -> ServerStatus:
        """Статус сервера у этого потребителя (спека §5.5)."""
        key = _session_key_of(entry)
        with self._lock:
            connected = self._connected(key)
            attempting = self._attempting(key)
            own = self._failures.get(key)
            retry = self._retry.get(key, 0)
        if connected:
            return ServerStatus(
                STATE_CONNECTED, checked_at=entry.checked_at, tools=entry.tools
            )
        if attempting:
            return ServerStatus(
                STATE_CONNECTING,
                from_catalog=entry.tools_known,
                checked_at=entry.checked_at,
                tools=entry.tools,
            )
        if own is not None and not (
            entry.tools_known and _is_interrupted(own)
        ):
            return _failed(own, entry, retry)
        return self._idle_status(entry)

    def _idle_status(self, entry: ServerEntry) -> ServerStatus:
        """Своей попытки и своего отказа нет (или попытка прервана при
        известных тулах)."""
        if entry.tools_known:
            return ServerStatus(
                STATE_CACHED,
                from_catalog=True,
                checked_at=entry.checked_at,
                tools=entry.tools,
            )
        shared = _shared_failure(entry)
        if shared is not None:
            fresh = self._service._clock() - shared.at < ATTEMPT_INTERVAL_SEC
            if fresh:
                return _failed(shared, entry)
        # Первая попытка начнётся в той же сверке.
        return ServerStatus(STATE_CONNECTING, checked_at=entry.checked_at)

    async def aclose(self) -> None:
        """Отказать в новой работе, снять попытки и серии, дождаться
        закрытия остатков, закрыть все сессии потребителя.

        Каждый вызов проходит весь путь: шаги идемпотентны, поэтому
        повторный вызов (в том числе после прерванного) доделывает
        остаток, а не выходит сразу.
        """
        self._service._bind_loop()
        with self._lock:
            self.closed = True
            work = [*self._connects.values(), *self._series.values()]
        try:
            for task in work:
                task.cancel()
            await asyncio.gather(*work, return_exceptions=True)
            with self._lock:
                closing = list(self._closing)
            await asyncio.gather(*closing, return_exceptions=True)
        finally:
            await self._aclose_sessions()

    async def _aclose_sessions(self) -> None:
        """Сессии владельца во всех реестрах; шаги не зависят друг от
        друга — прерывание одного не отменяет остальных."""
        async with self._closing_sessions:
            with self._lock:
                self._sessions.clear()
                self._ok.clear()
            with use_owner(self.owner):
                try:
                    await PersistentStdioRegistry.instance().aclose_all()
                finally:
                    try:
                        await stream_client._SESSION_MANAGER.aclose_all(
                            in_owner_task=False
                        )
                    finally:
                        forget_owner_sessions()
                        self._service._forget_consumer(self)


__all__ = [
    "ATTEMPT_INTERVAL_SEC",
    "RETRY_PAUSES_SEC",
    "STATE_CACHED",
    "STATE_CONNECTED",
    "STATE_CONNECTING",
    "STATE_FAILED",
    "McpConsumer",
    "ServerStatus",
]
