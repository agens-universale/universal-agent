"""Соединения потребителя сервиса MCP с серверами его набора.

``McpConnections`` — единственный владелец состояния потребителя и его
замка: набор и номер сборки, сессии и удачи без постоянной сессии, попытки
подключения, свои отказы, серии повторов, закрытия сессий, поколения
соединений и идущие вызовы. Сверка набора (фасад ``McpConsumer``) и
исполнитель вызова (``McpCallExecutor``) меняют это состояние только его
методами.

Подключить сервер — открыть его сессию под владельцем потребителя:
``initialize`` и ``tools/list`` по тому же определению и ключу сессии, что
получат обёртки тулов; эта же сессия потом обслуживает вызовы. Исход
подключения обновляет общую запись сервиса, а статус сервера у каждого
потребителя свой: собственная сессия, собственная попытка, собственный
отказ.

Состояние меняется только на цикле сервиса и под ``_lock``: статус читают
и с других потоков. Фоновая работа (попытки, серии повторов, перечитывание
списка тулов, закрытие сессий) идёт задачами с пустым контекстом
переменных — пользовательский стоп хода и чужой владелец туда не попадают;
владельца и слоты потребитель ставит явно. В stdout она не пишет: только
лог и статус.
"""

import asyncio
import contextvars
import dataclasses
import logging
import threading
from functools import partial
from typing import TYPE_CHECKING, Any, Callable

from gc_core.mcp_session_owner import use_owner
from gc_core.settings import settings_of
from gc_core.tools.connect_mcp import stream_client
from gc_core.tools.connect_mcp.abandonable import use_call_slots
from gc_core.tools.connect_mcp.agent_tools import mcp_cache_config
from gc_core.tools.connect_mcp.client import (
    forget_owner_sessions,
    forget_session,
)
from gc_core.tools.connect_mcp.consumer_status import (
    ATTEMPT_INTERVAL_SEC,
    INTERRUPTED_REASON,
    ServerStatus,
    server_status,
    shared_failure,
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

_SLOTS_PER_CONSUMER = 8
#: Внутренний срок списка длиннее внешнего срока подключения: истёкшее
#: время подключения приходит как ``TimeoutError`` внешнего ожидания.
_LIST_SLACK_SEC = 5
_INTERNAL_ERROR = "внутренняя ошибка подключения"


def session_key_of(entry: ServerEntry) -> str:
    """Ключ сессии потребителя для записи сервера."""
    return session_key(entry.key, entry.fingerprint)


def _describe(exc: BaseException, entry: ServerEntry) -> str:
    """Тип и текст исключения для лога — без значений секретов.

    Трейсбэк в лог не идёт: в цепочке исключений лежит исходная ошибка
    подключения, а тексты ошибок httpx несут адрес со строкой запроса.
    """
    return f"{type(exc).__name__}: {without_secrets(str(exc), entry.cfg)}"


async def _alist_in_time(
    server_key: str, cfg: dict, timeout: float
) -> list[dict] | None:
    """Список тулов в срок ``timeout``; не уложился — ``None``.

    Истёкший срок снимает идущий запрос, но не соединение: постоянная
    сессия переживает отмену запроса. Внутренний срок списка длиннее
    внешнего, поэтому ``TimeoutError`` транспорта (сбой) отличим от
    истёкшего срока.
    """
    limit = asyncio.timeout(timeout)
    try:
        async with limit:
            return await alist_raw_tools(
                server_key, cfg, timeout=timeout + _LIST_SLACK_SEC
            )
    except TimeoutError:
        if limit.expired():
            return None
        raise


def _succeeded(task: asyncio.Task) -> bool:
    """Попытка завершилась удачей (не отменена, без исключения, истина)."""
    return not task.cancelled() and task.exception() is None and task.result()


class McpConnections:
    """Соединения одного потребителя и всё его состояние под замком.

    Состояние по серверам ключуется ключом сессии (ключ сервера и
    отпечаток): ``_sessions`` — постоянная сессия есть; ``_ok`` — удача у
    сервера без постоянной сессии; ``_connects`` — идущая попытка;
    ``_series`` — серия повторов; ``_refreshes`` — идущее перечитывание
    списка тулов; ``_relisted`` — начало своего последнего перечитывания
    (неудачное повторяется не раньше срока свежести); ``_failures`` — своя
    последняя неудачная или прерванная попытка; ``_retry`` — номер повтора
    серии; ``_closing`` — закрытие остатков сессий после отказа или отмены
    попытки и сессий серверов, ушедших из набора; ``_closing_servers`` — те
    же закрытия по ключу сервера: новое подключение сервера дожидается всех
    его закрытий, какого бы отпечатка они ни были (у изменённого сервера
    старый процесс ещё держит порт или профиль).
    ``_servers`` — набор (записи серверов последней начатой сборки),
    ``_build`` — последний выданный номер сборки, ``_settings`` —
    настройки сборки, по которым строятся определения. ``_inflight`` —
    идущие вызовы тулов (их снимает закрытие потребителя).
    ``_generations`` — поколение соединения по ключу сессии: растёт при
    каждом снятии соединения (сбой, отмена, уход сервера). Вызов
    запоминает его на старте: изменилось к сбою — сессию закрыл сам
    потребитель, и сбой не отказ сервера.
    """

    def __init__(self, service: "McpService", owner: str, label: str) -> None:
        self._service = service
        self.owner = owner
        self.label = label
        self.closed = False
        self.slots = threading.BoundedSemaphore(_SLOTS_PER_CONSUMER)
        self._settings: Any = None
        self._lock = threading.Lock()
        self._sessions: set[str] = set()
        self._ok: set[str] = set()
        self._connects: dict[str, asyncio.Task] = {}
        self._series: dict[str, asyncio.Task] = {}
        self._refreshes: dict[str, asyncio.Task] = {}
        self._relisted: dict[str, float] = {}
        self._failures: dict[str, Failure] = {}
        self._retry: dict[str, int] = {}
        self._closing: set[asyncio.Task] = set()
        self._closing_servers: dict[str, set[asyncio.Task]] = {}
        self._build = 0
        self._servers: dict[str, ServerEntry] = {}
        self._inflight: set[asyncio.Task] = set()
        self._generations: dict[str, int] = {}
        # Хвост закрытия (сессии владельца) идёт по одному вызову за раз.
        self._closing_sessions = asyncio.Lock()

    def wrapper_cfg(self, entry: ServerEntry) -> dict:
        """Определение для подключения и обёрток тулов.

        Из настроек сборки (алиасы и trace этой сборки), с умолчаниями
        сервиса и ключом сессии потребителя.
        """
        servers = mcp_cache_config(self._settings)["mcp-servers"]
        cfg = with_service_defaults(servers[entry.key])
        cfg[SESSION_KEY_FIELD] = session_key_of(entry)
        return cfg

    def discovery_timeout(self) -> float:
        """Срок подключения по настройкам последней сверки."""
        return settings_of(self._settings).mcp.meta.discovery_timeout_sec

    def tools_ttl(self) -> float:
        """Срок свежести списка тулов по настройкам последней сверки."""
        return settings_of(self._settings).mcp.meta.tools_cache_ttl

    def has_session(self, entry: ServerEntry) -> bool:
        with self._lock:
            return session_key_of(entry) in self._sessions

    def _connected(self, key: str) -> bool:
        """Сессия есть или удача без постоянной сессии. Звать под
        ``_lock``."""
        return key in self._sessions or key in self._ok

    def _attempting(self, key: str) -> bool:
        """Идёт своя попытка. Звать под ``_lock``."""
        task = self._connects.get(key)
        return task is not None and not task.done()

    def attempt_of(
        self, entry: ServerEntry
    ) -> tuple[bool, asyncio.Task | None]:
        """Под замком: подключён ли сервер и задача идущей своей попытки
        (``None``, если попытки нет)."""
        key = session_key_of(entry)
        with self._lock:
            if self._connected(key):
                return True, None
            task = self._connects.get(key) if self._attempting(key) else None
        return False, task

    def own_failure(self, entry: ServerEntry) -> Failure | None:
        """Под замком: своя последняя неудачная или прерванная попытка."""
        with self._lock:
            return self._failures.get(session_key_of(entry))

    def may_connect(self, entry: ServerEntry) -> bool:
        """Правило 30 с: своей попытки нет, с последнего отказа — 30 с.

        Последний отказ — более позднее из своей неудачной или прерванной
        попытки и отказа записи (если он позже удачи записи).
        """
        key = session_key_of(entry)
        with self._lock:
            if self._attempting(key):
                return False
            own = self._failures.get(key)
        failures = (own, shared_failure(entry))
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
        task = self.start_attempt(entry)
        waiting = asyncio.get_running_loop().create_future()

        def settle(done: asyncio.Task) -> None:
            if not waiting.done():
                waiting.set_result(_succeeded(done))

        task.add_done_callback(settle)
        return waiting

    def start_attempt(self, entry: ServerEntry) -> asyncio.Task:
        """Задача попытки (идущая или новая); после ``aclose`` —
        ``McpServiceClosed``. Ждать её — только через ``asyncio.wait``."""
        self._service._bind_loop()
        key = session_key_of(entry)
        with self._lock:
            if self.closed:
                raise McpServiceClosed(SERVICE_CLOSED_TEXT)
            running = self._connects.get(key)
            if running is not None and not running.done():
                return running
            started = self._service._clock()
            # Закрытия прежних сессий этого сервера (его убрали и вернули
            # или изменили) должны кончиться до новой сессии: иначе они
            # закрыли бы её, а старый процесс держал бы исключительный
            # ресурс (порт, профиль браузера). Ждём только закрытия,
            # начатые до попытки, — циклов ожидания нет.
            closing = set(self._closing_servers.get(entry.key, ()))
            task = asyncio.get_running_loop().create_task(
                self._aconnect(entry, started, closing),
                context=contextvars.Context(),
            )
            self._connects[key] = task
        task.add_done_callback(partial(self._on_connect_done, entry, started))
        return task

    async def _aconnect(
        self,
        entry: ServerEntry,
        started: float,
        closing: set[asyncio.Task],
    ) -> bool:
        with use_owner(self.owner), use_call_slots(self.slots):
            try:
                if closing:
                    await asyncio.wait(closing)
                await self._alist_and_record(entry)
                return True
            except asyncio.CancelledError:
                await self.adrop(entry)
                raise
            except Exception as exc:
                # Сначала снять сессию и закрыть остаток: если запись
                # отказа сама бросит, ключ не останется «подключённым».
                await self.adrop(entry)
                self._record_failure(entry, exc, started)
                return False

    async def _alist_and_record(self, entry: ServerEntry) -> None:
        cfg = self.wrapper_cfg(entry)
        timeout = self.discovery_timeout()
        raw = await asyncio.wait_for(
            alist_raw_tools(entry.key, cfg, timeout=timeout + _LIST_SLACK_SEC),
            timeout,
        )
        self._service.record_success(entry, raw)
        key = session_key_of(entry)
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
            self._failures[session_key_of(entry)] = dataclasses.replace(
                failure, at=started
            )

    def _drop_connection(self, key: str) -> None:
        with self._lock:
            self._forget_connection(key)

    def _forget_connection(self, key: str) -> None:
        """Снять соединение и сменить его поколение. Звать под ``_lock``."""
        self._sessions.discard(key)
        self._ok.discard(key)
        self._generations[key] = self._generations.get(key, 0) + 1

    async def adrop(self, entry: ServerEntry) -> None:
        """Снять соединение (под замком, со сменой поколения) и закрыть
        остаток сессии."""
        self._drop_connection(session_key_of(entry))
        await self._aclose_rest(entry)

    async def _aclose_rest(self, entry: ServerEntry) -> None:
        """Закрыть остаток сессии; повторная отмена закрытие не обрывает."""
        task = asyncio.get_running_loop().create_task(
            self._aclose_session(entry), context=contextvars.Context()
        )
        self._track_closing(task, entry.key)
        await asyncio.shield(task)

    def _track_closing(self, task: asyncio.Task, server_key: str) -> None:
        """Закрытие числится за потребителем (его ждёт ``aclose``) и за
        ключом сервера (его ждёт новое подключение этого сервера)."""
        with self._lock:
            self._closing.add(task)
            self._closing_servers.setdefault(server_key, set()).add(task)
        task.add_done_callback(partial(self._forget_closing, server_key))

    def _forget_closing(self, server_key: str, task: asyncio.Task) -> None:
        with self._lock:
            self._closing.discard(task)
            pending = self._closing_servers.get(server_key, set())
            pending.discard(task)
            if not pending:
                self._closing_servers.pop(server_key, None)

    async def _aclose_session(self, entry: ServerEntry) -> None:
        """Сессия сервера у потребителя — во всех трёх реестрах."""
        key = session_key_of(entry)
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
            # Идентификатор legacy-http ключуется именем сервера: у
            # изменённого сервера (тот же ключ, другой отпечаток) он уже
            # принадлежит новой записи набора.
            with self._lock:
                current = self._servers.get(entry.key)
            if current is None or current is entry:
                forget_session(entry.key)

    def _on_connect_done(
        self, entry: ServerEntry, started: float, task: asyncio.Task
    ) -> None:
        key = session_key_of(entry)
        with self._lock:
            # Отмена закрытием потребителя — «подключение прервано» (идёт
            # в правило 30 с); попытку, снятую сверкой, ``leave`` уже
            # убрал из ``_connects``: её отмена отказом не считается (§5.3).
            own = self._connects.get(key) is task
            if own:
                del self._connects[key]
            if task.cancelled():
                if own:
                    self._failures[key] = Failure(
                        started, "other", INTERRUPTED_REASON
                    )
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
            self._failures[session_key_of(entry)] = failure
        return True

    def _retries(self, entry: ServerEntry) -> bool:
        """Повторы — у сетевого сервера и потребителя «на старте»."""
        return self._service.connect_on_start and not is_process_transport(
            entry.cfg
        )

    def _start_series(self, entry: ServerEntry) -> None:
        key = session_key_of(entry)
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
        key = session_key_of(entry)
        for attempt, pause in enumerate(RETRY_PAUSES_SEC, start=1):
            with self._lock:
                if self.closed:
                    return
                self._retry[key] = attempt
            await self._service._sleep(pause)
            with self._lock:
                # За паузу сервер мог подключиться (вызов, вход в ход):
                # попытка поверх живой сессии её бы и закрыла при отказе.
                # Сервер мог и уйти из набора: серию заводит колбэк
                # попытки, и сверка могла пройти раньше него.
                if self.closed or self._connected(key):
                    return
                if self._servers.get(entry.key) is not entry:
                    return
            # Ждём без отмены попытки: снятие серии её не прерывает.
            task = self.start_attempt(entry)
            await asyncio.wait({task})
            if _succeeded(task):
                return

    def _on_series_done(self, entry: ServerEntry, task: asyncio.Task) -> None:
        key = session_key_of(entry)
        with self._lock:
            if self._series.get(key) is task:
                del self._series[key]
                self._retry.pop(key, None)
        if not task.cancelled():
            self._unexpected_failure(entry, task)

    def start_turn(self, entry: ServerEntry) -> None:
        """Вход в ход по серверу набора (спека §5.5); ничего не ждёт.

        Подключённый сетевой сервер, чей список проверен раньше, чем
        ``tools_ttl()`` назад, перечитывает список в фоне. Неподключённый
        сервер у потребителя «на старте» без своей попытки и без серии
        повторов получает попытку по правилу 30 с. Закрытый потребитель
        ничего не начинает.
        """
        key = session_key_of(entry)
        with self._lock:
            if self.closed:
                return
            connected = self._connected(key)
            idle = not self._attempting(key) and key not in self._series
            relist = self._new_relist(entry, key) if connected else None
        if relist is not None:
            relist.add_done_callback(partial(self._on_relist_done, entry))
        elif connected or not idle or not self._service.connect_on_start:
            return
        elif self.may_connect(entry):
            self.start_attempt(entry)

    def _new_relist(self, entry: ServerEntry, key: str) -> asyncio.Task | None:
        """Задача перечитывания списка, если оно назрело. Звать под
        ``_lock``.

        Только у сетевых транспортов: у процессных список приходит от
        того же процесса с каждым подключением. Назрело — с последней
        проверки записи и своего последнего перечитывания прошло больше
        ``tools_ttl()``.
        """
        if is_process_transport(entry.cfg) or key in self._refreshes:
            return None
        now = self._service._clock()
        marks = (entry.checked_at, self._relisted.get(key))
        since = [mark for mark in marks if mark is not None]
        if since and now - max(since) <= self.tools_ttl():
            return None
        self._relisted[key] = now
        task = asyncio.get_running_loop().create_task(
            self._arelist(entry, self._generations.get(key, 0)),
            context=contextvars.Context(),
        )
        self._refreshes[key] = task
        return task

    async def _arelist(self, entry: ServerEntry, generation: int) -> None:
        """Перечитать список тулов подключённого сервера (свежесть, §5.5).

        Список идёт по живой постоянной сессии, без неё — разовым
        запросом; удача обновляет запись (тулы, время проверки, каталог),
        статус не меняется. Истёкший срок — не отказ: запрос снят, сессия
        и статус остаются, перечитывание повторится через ``tools_ttl()``.
        Иной сбой — отказ связи (``afail``). Исход не принимается, если
        соединение за время списка сняли (сверка, сбой вызова, закрытие).
        """
        cfg = self.wrapper_cfg(entry)
        timeout = self.discovery_timeout()
        with use_owner(self.owner), use_call_slots(self.slots):
            try:
                raw = await _alist_in_time(entry.key, cfg, timeout)
            except Exception as exc:
                if not self._relist_lost(entry, generation):
                    await self._afail_relist(entry, exc)
                return
        if raw is None:
            logger.info(
                "MCP %s: список тулов для потребителя %s не пришёл за %s с; "
                "сессия остаётся",
                entry.key,
                self.label,
                timeout,
            )
        elif not self._relist_lost(entry, generation):
            self._service.record_success(entry, raw)

    def _relist_lost(self, entry: ServerEntry, generation: int) -> bool:
        """Под замком: потребитель закрыт, сервер ушёл из набора или
        соединение снято (поколение сменилось с ``generation``)."""
        key = session_key_of(entry)
        with self._lock:
            return (
                self.closed
                or self._servers.get(entry.key) is not entry
                or self._generations.get(key, 0) != generation
            )

    async def _afail_relist(
        self, entry: ServerEntry, exc: BaseException
    ) -> None:
        failure = await self.afail(entry, exc)
        if failure is not None:
            logger.warning(
                "MCP %s: перечитывание списка тулов потребителя %s не "
                "удалось: %s",
                entry.key,
                self.label,
                failure.reason,
            )

    def _on_relist_done(self, entry: ServerEntry, task: asyncio.Task) -> None:
        key = session_key_of(entry)
        with self._lock:
            if self._refreshes.get(key) is task:
                del self._refreshes[key]
        if not task.cancelled():
            self._unexpected_failure(entry, task)

    def next_build(self) -> int:
        """Номер новой сборки; набор меняет только сверка самой поздней."""
        with self._lock:
            self._build += 1
            return self._build

    def is_stale(self, build: int) -> bool:
        """Под замком: сборка выдана раньше самой поздней."""
        with self._lock:
            return build < self._build

    def is_latest(self, build: int) -> bool:
        """Под замком: сборка — самая поздняя из выданных."""
        with self._lock:
            return build == self._build

    def servers(self) -> dict[str, ServerEntry]:
        """Набор: записи серверов последней начатой сборки."""
        with self._lock:
            return dict(self._servers)

    def current(self, server_key: str) -> ServerEntry | None:
        """Под замком: запись сервера в наборе."""
        with self._lock:
            return self._servers.get(server_key)

    def refused(self) -> bool:
        """Потребитель закрыт или сервис остановлен."""
        return self.closed or self._service.closed

    def replace_set(
        self, build: int, settings: Any, entries: dict[str, ServerEntry]
    ) -> list[ServerEntry] | None:
        """Заменить набор, если сборка не устарела; вернуть убранные и
        изменённые записи (устаревшая сборка — ``None``). Номер сборки
        проверяется под тем же замком, под которым меняется набор."""
        with self._lock:
            if build < self._build:
                return None
            gone = [
                entry
                for key, entry in self._servers.items()
                if entries.get(key) is not entry
            ]
            self._servers = entries
            self._settings = settings
        return gone

    def leave(self, entry: ServerEntry) -> None:
        """Сервер ушёл из набора: снять его попытку, серию и перечитывание,
        сессию закрыть в фоне под владельцем потребителя.

        Начатый по этой сессии вызов получает ошибку закрытой сессии, а
        не зависает. Закрытие дожидается снятых задач и прежних закрытий
        этого сервера: так закрытия одного сервера идут по порядку.
        """
        self._withdraw(entry)

    def fail_unready(self, entry: ServerEntry) -> None:
        """Подключение не уложилось в срок сборки, которая ждёт все
        серверы (политика ``wait_all``): отказ ``timeout`` — в запись и
        свой, попытка снимается, сессия закрывается в фоне, как у
        ушедшего сервера.

        Отказ ставится под тем же замком, что снимает попытку: статус
        переходит из ``connecting`` сразу в ``failed``, тулы каталога в
        срез не попадают. Снятая попытка в правило 30 с «прерванной» не
        идёт и серию повторов не заводит — отказ уже записан. Попытки нет
        (исход уже есть) или потребитель закрыт — ничего.
        """
        key = session_key_of(entry)
        with self._lock:
            task = self._connects.get(key)
            if self.closed or task is None or task.done():
                return
        failure = self._service.record_failure(entry, TimeoutError())
        self._withdraw(entry, failure)

    def _withdraw(
        self, entry: ServerEntry, failure: Failure | None = None
    ) -> None:
        """Снять попытку, серию и перечитывание сервера, его соединение —
        под замком (с отказом ``failure``, если он есть), сессию закрыть в
        фоне после снятых задач и прежних закрытий этого сервера."""
        key = session_key_of(entry)
        with self._lock:
            work = {
                self._connects.pop(key, None),
                self._series.pop(key, None),
                self._refreshes.pop(key, None),
            }
            work.discard(None)
            earlier = set(self._closing_servers.get(entry.key, ()))
            self._retry.pop(key, None)
            self._relisted.pop(key, None)
            self._forget_connection(key)
            if failure is not None:
                self._failures[key] = failure
        for task in work:
            task.cancel()
        closing = asyncio.get_running_loop().create_task(
            self._aclose_left(entry, work | earlier),
            context=contextvars.Context(),
        )
        self._track_closing(closing, entry.key)

    async def _aclose_left(
        self, entry: ServerEntry, work: set[asyncio.Task]
    ) -> None:
        if work:
            await asyncio.wait(work)
        # Отмена могла не дойти до попытки (готовый результат
        # ``wait_for`` на 3.11): тогда она успела записать сессию.
        self._drop_connection(session_key_of(entry))
        await self._aclose_session(entry)

    def leave_if_gone(self, entry: ServerEntry) -> None:
        """Шаг 8: сервер ушёл из набора, пока шёл вызов, — закрыть сессию,
        которую вызов мог открыть заново (§5.3)."""
        with self._lock:
            gone = (
                not self.closed and self._servers.get(entry.key) is not entry
            )
        if gone:
            self.leave(entry)

    def status_of(self, entry: ServerEntry) -> ServerStatus:
        """Статус сервера у этого потребителя (спека §5.5)."""
        key = session_key_of(entry)
        with self._lock:
            connected = self._connected(key)
            attempting = self._attempting(key)
            own = self._failures.get(key)
            retry = self._retry.get(key, 0)
            closed = self.closed
        return server_status(
            entry,
            connected=connected,
            attempting=attempting,
            own=own,
            retry=retry,
            now=self._service._clock(),
            closed=closed,
        )

    def begin_call(self, task: asyncio.Task, entry: ServerEntry) -> int:
        """Под замком: вызов — в учёт идущих (его снимет ``aclose``);
        отдаёт поколение соединения на старте вызова."""
        with self._lock:
            self._inflight.add(task)
            return self._generations.get(session_key_of(entry), 0)

    def end_call(self, task: asyncio.Task) -> None:
        """Под замком: снять вызов с учёта идущих."""
        with self._lock:
            self._inflight.discard(task)

    def confirm(self, entry: ServerEntry, cfg: dict, generation: int) -> None:
        """Шаг 5: удачный вызов подтверждает соединение. Постоянную сессию
        — только если её за время вызова никто не снял (поколение то же):
        иначе ключ вернул бы снятую сессию, и следующий вызов открыл бы её
        мимо ожидания закрытий. У сервера без постоянной сессии снимать
        нечего: его ответ и есть связь."""
        key = session_key_of(entry)
        persistent = bool(cfg.get("persistent_session"))
        with self._lock:
            if persistent and self._generations.get(key, 0) != generation:
                return
            self._failures.pop(key, None)
            (self._sessions if persistent else self._ok).add(key)

    def call_lost(
        self, entry: ServerEntry, cfg: dict, generation: int
    ) -> tuple[bool, bool]:
        """Под замком: сервер ушёл из набора; постоянную сессию за время
        вызова снял сам потребитель (поколение сменилось с ``generation``).
        """
        key = session_key_of(entry)
        with self._lock:
            gone = self._servers.get(entry.key) is not entry
            lost = (
                cfg.get("persistent_session")
                and self._generations.get(key, 0) != generation
            )
        return gone, bool(lost)

    async def afail(
        self, entry: ServerEntry, exc: BaseException
    ) -> Failure | None:
        """Шаг 7, общий для отказов связи: снять соединение и закрыть
        сессию; потребитель закрыт — ``None``; иначе отказ — в запись и
        свой (текст — ``classify_failure``, без секретов), сетевой сервер у
        потребителя «на старте» переподключается в фоне; отдаёт отказ."""
        await self.adrop(entry)
        if self.refused():
            return None
        failure = self._service.record_failure(entry, exc)
        with self._lock:
            self._failures[session_key_of(entry)] = failure
        if self._retries(entry):
            self.start_attempt(entry)
        return failure

    async def aclose(self, finish: Callable[[], None]) -> None:
        """Отказать в новой работе, снять попытки, серии, перечитывания
        списка и идущие вызовы, дождаться закрытия остатков, закрыть все
        сессии потребителя.

        Каждый вызов проходит весь путь: шаги идемпотентны, поэтому
        повторный вызов (в том числе после прерванного) доделывает
        остаток, а не выходит сразу. ``finish`` — последний шаг хвоста
        закрытия, под его замком, только если сессии закрылись без
        исключения и прерывания.

        ``CancelledError``, когда вызывающего не отменяли (отменилось
        закрытие одной сессии реестра постоянного stdio), — в лог, не
        наружу: закрытие не завершено, потребитель остаётся за сервисом,
        и его дозакроет закрытие сервиса. Отмена вызывающего — отмена.
        """
        self._service._bind_loop()
        cancels = asyncio.current_task().cancelling()
        with self._lock:
            self.closed = True
            work = [
                *self._connects.values(),
                *self._series.values(),
                *self._refreshes.values(),
                *self._inflight,
            ]
        try:
            for task in work:
                task.cancel()
            await asyncio.gather(*work, return_exceptions=True)
            with self._lock:
                closing = list(self._closing)
            await asyncio.gather(*closing, return_exceptions=True)
        finally:
            await self._aclose_sessions_once(finish, cancels)

    async def _aclose_sessions_once(
        self, finish: Callable[[], None], cancels: int
    ) -> None:
        """Хвост закрытия; чужая отмена (счётчик отмен задачи не вырос с
        ``cancels``) — в лог."""
        try:
            await self._aclose_sessions(finish)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() > cancels:
                raise
            logger.warning(
                "MCP: закрытие сессий потребителя %s не завершено: "
                "отменилось закрытие сессии",
                self.label,
            )

    async def _aclose_sessions(self, finish: Callable[[], None]) -> None:
        """Сессии владельца во всех реестрах; шаги не зависят друг от
        друга — прерывание одного не отменяет остальных. ``finish`` —
        только после всех шагов без исключения: иначе снятый с учёта
        потребитель с незакрытыми сессиями не закрыл бы и сервис."""
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
            finish()


__all__ = ["RETRY_PAUSES_SEC", "McpConnections", "session_key_of"]
