"""Потребитель сервиса MCP: подключение своей сессией, свой статус и
исполнитель вызова тулов.

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

Вызов тула модели идёт через ``_acall`` (спека §5.4): он ждёт своё
подключение, идёт по сессии потребителя под его владельцем и слотами, а
потерю сессии, смерть сервера, срок и уход сервера из набора отдаёт ошибкой
тула; чистые части исполнителя — в ``consumer_call``.
"""

import asyncio
import contextvars
import dataclasses
import logging
import threading
from functools import partial
from typing import TYPE_CHECKING, Any, Awaitable, Callable, NoReturn

from gc_core.config_modules.field_meta import plain_dump
from gc_core.mcp_session_owner import new_owner_token, use_owner
from gc_core.settings import settings_of
from gc_core.tools.connect_mcp import stream_client
from gc_core.tools.connect_mcp.abandonable import use_call_slots
from gc_core.tools.connect_mcp.agent_tools import mcp_cache_config
from gc_core.tools.connect_mcp.api import acall_mcp_tool
from gc_core.tools.connect_mcp.client import (
    forget_owner_sessions,
    forget_session,
)
from gc_core.tools.connect_mcp.consumer_call import (
    SERVER_GONE_TEXT,
    SESSION_LOST_TEXT,
    await_attempt,
    call_target,
    is_timeout,
    is_transport_failure,
    refusal_text,
    timeout_error,
    tool_error,
)
from gc_core.tools.connect_mcp.consumer_status import (
    ATTEMPT_INTERVAL_SEC,
    INTERRUPTED_REASON,
    ServerStatus,
    server_status,
    shared_failure,
)
from gc_core.tools.connect_mcp.consumer_view import (
    McpToolsView,
    aadd_backend_part,
    add_server,
    backend_config,
    callers_pins,
)
from gc_core.tools.connect_mcp.effective import effective_mcp_servers
from gc_core.tools.connect_mcp.execute_on import runs_in_agent_runtime
from gc_core.tools.connect_mcp.persistent_stdio import (
    PersistentStdioRegistry,
    is_persistent_stdio,
)
from gc_core.tools.connect_mcp.registry import ToolsCache, alist_raw_tools
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
from gc_core.user_stop import current_user_stop

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


def _session_key_of(entry: ServerEntry) -> str:
    return session_key(entry.key, entry.fingerprint)


def _describe(exc: BaseException, entry: ServerEntry) -> str:
    """Тип и текст исключения для лога — без значений секретов.

    Трейсбэк в лог не идёт: в цепочке исключений лежит исходная ошибка
    подключения, а тексты ошибок httpx несут адрес со строкой запроса.
    """
    return f"{type(exc).__name__}: {without_secrets(str(exc), entry.cfg)}"


def _succeeded(task: asyncio.Task) -> bool:
    """Попытка завершилась удачей (не отменена, без исключения, истина)."""
    return not task.cancelled() and task.exception() is None and task.result()


def _agent_side_definitions(settings: Any, os_backend: Any) -> dict[str, dict]:
    """Определения серверов сборки без trace (``plain_dump`` по алиасам)
    — кроме серверов, исполняемых внутри бэкенда (§5.10)."""
    defs = {
        key: plain_dump(model, by_alias=True)
        for key, model in effective_mcp_servers(settings).items()
    }
    return {
        key: cfg
        for key, cfg in defs.items()
        if runs_in_agent_runtime(cfg, os_backend=os_backend)
    }


def _pin_view(pinned: dict[str, tuple[ServerEntry, ServerStatus]]) -> dict:
    """То в срезе, что меняет набор тулов: без времени и номера повтора."""
    return {
        key: (
            entry.fingerprint,
            st.state,
            st.reason_code,
            st.from_catalog,
            st.tools,
        )
        for key, (entry, st) in pinned.items()
    }


class McpConsumer:
    """Сессии одного потребителя сервиса и его статусы серверов.

    Состояние по серверам ключуется ключом сессии (ключ сервера и
    отпечаток): ``_sessions`` — постоянная сессия есть; ``_ok`` — удача у
    сервера без постоянной сессии; ``_connects`` — идущая попытка;
    ``_series`` — серия повторов; ``_failures`` — своя последняя неудачная
    или прерванная попытка; ``_retry`` — номер повтора серии; ``_closing`` —
    закрытие остатков сессий после отказа или отмены попытки и сессий
    серверов, ушедших из набора; ``_closing_servers`` — те же закрытия по
    ключу сервера: новое подключение сервера дожидается всех его закрытий,
    какого бы отпечатка они ни были (у изменённого сервера старый процесс
    ещё держит порт или профиль).
    ``_servers`` — набор (записи серверов последней начатой сборки),
    ``_build`` — последний выданный номер сборки, ``_pinned`` —
    закреплённый срез ``{ключ: (запись, статус)}``.
    ``_settings`` — настройки сборки, по которым строятся определения.
    ``_backend_cache`` — кэш тулов серверов, исполняемых внутри бэкенда
    сборки (§5.10), пока бэкенд сборки — тот же объект. ``_inflight`` —
    идущие вызовы тулов (их снимает закрытие потребителя).
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
        self._closing_servers: dict[str, set[asyncio.Task]] = {}
        self._build = 0
        self._servers: dict[str, ServerEntry] = {}
        self._pinned: dict[str, tuple[ServerEntry, ServerStatus]] = {}
        self._backend_cache: ToolsCache | None = None
        self._inflight: set[asyncio.Task] = set()
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
        key = _session_key_of(entry)
        with use_owner(self.owner), use_call_slots(self._slots):
            try:
                if closing:
                    await asyncio.wait(closing)
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
        key = _session_key_of(entry)
        with self._lock:
            # Отмена закрытием потребителя — «подключение прервано» (идёт
            # в правило 30 с); попытку, снятую сверкой, ``_leave`` уже
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
                # Сервер мог и уйти из набора: серию заводит колбэк
                # попытки, и сверка могла пройти раньше него.
                if self.closed or self._connected(key):
                    return
                if self._servers.get(entry.key) is not entry:
                    return
            # Ждём без отмены попытки: снятие серии её не прерывает.
            task = self._start_attempt(entry)
            await asyncio.wait({task})
            if _succeeded(task):
                return

    def _on_series_done(self, entry: ServerEntry, task: asyncio.Task) -> None:
        key = _session_key_of(entry)
        with self._lock:
            if self._series.get(key) is task:
                del self._series[key]
                self._retry.pop(key, None)
        if not task.cancelled():
            self._unexpected_failure(entry, task)

    def next_build(self) -> int:
        """Номер новой сборки; набор меняет только сверка самой поздней."""
        with self._lock:
            self._build += 1
            return self._build

    def servers(self) -> dict[str, ServerEntry]:
        """Набор: записи серверов последней начатой сборки."""
        with self._lock:
            return dict(self._servers)

    def pin(self) -> bool:
        """Закрепить срез ``{ключ: (запись, статус)}`` набора.

        Возвращает, отличается ли он от прошлого закреплённого по
        отпечатку, состоянию, коду причины, признаку каталога и тулам;
        время проверки и номер повтора на пересборку не влияют.
        """
        pinned = {
            key: (entry, self.status_of(entry))
            for key, entry in self.servers().items()
        }
        previous, self._pinned = self._pinned, pinned
        return _pin_view(previous) != _pin_view(pinned)

    async def aview(
        self, settings: Any, *, os_backend: Any = None
    ) -> McpToolsView:
        """Срез тулов для настроек вызывающего (спека §5.2).

        Серверы сервиса — из закреплённого среза, только те, что есть в
        определениях вызывающего с тем же отпечатком; оформление тулов —
        по его настройкам. Серверы, исполняемые внутри бэкенда сборки, —
        как сегодня, через кэш тулов. Сети и ожиданий у серверов сервиса
        нет; закрытый потребитель отдаёт закреплённый срез.
        """
        definitions = _agent_side_definitions(settings, os_backend)
        config = mcp_cache_config(settings)
        view = McpToolsView({}, {}, {}, self._acall)
        for key, (entry, status) in callers_pins(
            self._pinned, definitions
        ).items():
            add_server(
                view,
                key,
                config["mcp-servers"][key],
                status,
                self.wrapper_cfg(entry),
            )
        backend = backend_config(config, definitions)
        if backend is not None:
            cache = self._backend_tools_cache(os_backend)
            await aadd_backend_part(view, cache, backend)
        return view

    def _backend_tools_cache(self, os_backend: Any) -> ToolsCache:
        """Кэш бэкенда сервиса, если он есть; иначе свой — с бэкендом
        сборки, новый, когда бэкенд сборки — другой объект."""
        shared = self._service.backend_tools_cache()
        if shared is not None:
            return shared
        cache = self._backend_cache
        if cache is None or cache.os_backend is not os_backend:
            cache = self._backend_cache = ToolsCache(os_backend=os_backend)
        return cache

    async def aensure(
        self, build: int, settings: Any, *, os_backend: Any = None
    ) -> None:
        """Сверить набор с серверами сборки ``build`` (спека §5.3–§5.4).

        Устаревшая сборка (выдана раньше самой поздней) ничего не меняет.
        Набор меняется атомарно, до любых ожиданий: новые серверы
        подключаются по политике хоста, убранные и изменённые закрываются
        в фоне, неизменные не трогаются. Затем сборка ждёт свои
        подключения серверов без тулов и без отказа в этом процессе (при
        ``wait_all`` — все свои подключения), не дольше
        ``discovery_timeout_sec``; отмена ожидания набор не откатывает.
        После остановки сервиса или закрытия — ``McpServiceClosed``.
        """
        self._service._bind_loop()
        self._refuse_if_closed()
        with self._lock:
            # Быстрый путь: устаревшая сборка не трогает записи и каталог.
            # Решающая проверка — в ``_replace_set``, под замком замены.
            stale = build < self._build
        if stale:
            return
        timeout = settings_of(settings).mcp.meta.discovery_timeout_sec
        entries = {
            key: self._service.entry(key, cfg, discovery_timeout=timeout)
            for key, cfg in _agent_side_definitions(
                settings, os_backend
            ).items()
        }
        gone = self._replace_set(build, settings, entries)
        if gone is None:
            return
        for entry in gone:
            self._leave(entry)
        self.pin()
        waits: set[asyncio.Task] = set()
        for entry in entries.values():
            task = self._connect_for_build(entry)
            if task is not None:
                waits.add(task)
        await self._await_build(waits, timeout)
        self._refuse_if_closed()
        with self._lock:
            latest = build == self._build
        # Обогнанная сборка не закрепляет срез: иначе вход в ход не увидел
        # бы изменения, случившегося после возврата новой сборки.
        if latest:
            self.pin()

    def _refuse_if_closed(self) -> None:
        if self.closed or self._service.closed:
            raise McpServiceClosed(SERVICE_CLOSED_TEXT)

    def _replace_set(
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

    def _leave(self, entry: ServerEntry) -> None:
        """Сервер ушёл из набора: снять его попытку и серию, сессию закрыть
        в фоне под владельцем потребителя.

        Начатый по этой сессии вызов получает ошибку закрытой сессии, а
        не зависает. Закрытие дожидается снятых задач и прежних закрытий
        этого сервера: так закрытия одного сервера идут по порядку.
        """
        key = _session_key_of(entry)
        with self._lock:
            work = {self._connects.pop(key, None), self._series.pop(key, None)}
            work.discard(None)
            earlier = set(self._closing_servers.get(entry.key, ()))
            self._retry.pop(key, None)
            self._sessions.discard(key)
            self._ok.discard(key)
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
        self._drop_connection(_session_key_of(entry))
        await self._aclose_session(entry)

    def _connect_for_build(self, entry: ServerEntry) -> asyncio.Task | None:
        """Начать подключение по политике хоста; отдать задачу, если
        сборке её ждать."""
        key = _session_key_of(entry)
        with self._lock:
            if self._connected(key):
                return None
            task = self._connects.get(key) if self._attempting(key) else None
        wanted = self._service.connect_on_start or not entry.tools_known
        if task is None and wanted and self.may_connect(entry):
            task = self._start_attempt(entry)
        if task is None or not self._build_waits(entry):
            return None
        return task

    def _build_waits(self, entry: ServerEntry) -> bool:
        """Сборка ждёт: тулов нет и отказа в этом процессе ещё не было;
        при ``wait_all`` — всегда."""
        if self._service.wait_all:
            return True
        return not entry.tools_known and entry.last_failure is None

    async def _await_build(
        self, tasks: set[asyncio.Task], timeout: float
    ) -> None:
        """Ждать попытки, остановку сервиса или срок — что раньше.

        Попытки ждём через ``asyncio.wait``: отмена ожидающего (срок,
        отмена сборки) их не прерывает.
        """
        if not tasks:
            return
        loop = asyncio.get_running_loop()
        stopped = loop.create_task(self._service._stopped_event.wait())
        settled = loop.create_task(asyncio.wait(tasks))
        try:
            await asyncio.wait(
                {stopped, settled},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stopped.cancel()
            settled.cancel()

    def status_of(self, entry: ServerEntry) -> ServerStatus:
        """Статус сервера у этого потребителя (спека §5.5)."""
        key = _session_key_of(entry)
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

    async def _acall(
        self,
        *,
        full_tool_name: str,
        arguments: Any,
        servers_cfg: dict[str, dict],
        tools_registry: dict[str, dict] | None,
        timeout: float,
        default_use_retry: bool,
        os_backend: Any,
    ) -> Any:
        """Исполнитель вызова тула потребителя (спека §5.4).

        Сервер бэкенда (без ключа сессии в определении) — прежним путём.
        Иначе: закрыт или сервис остановлен — «сервис MCP остановлен»;
        сервера с этим отпечатком нет в наборе — «сервер отключён»; вызов
        ждёт своё подключение (или начинает его по правилу 30 с) и идёт
        по сессии потребителя, под его владельцем и слотами.
        """
        call = partial(
            acall_mcp_tool,
            full_tool_name=full_tool_name,
            arguments=arguments,
            servers_cfg=servers_cfg,
            tools_registry=tools_registry,
            timeout=timeout,
            default_use_retry=default_use_retry,
            os_backend=os_backend,
        )
        server_key, key = call_target(
            full_tool_name, servers_cfg, tools_registry
        )
        if server_key is None or key is None:
            return await call()
        entry = self._call_entry(server_key, key)
        cfg = servers_cfg[server_key]
        await self._aready_for_call(entry, cfg)
        return await self._arun_call(entry, cfg, call, timeout)

    def _is_closed(self) -> bool:
        return self.closed or self._service.closed

    def _call_entry(self, server_key: str, key: str) -> ServerEntry:
        """Запись сервера вызова в наборе; иначе — ошибка тула."""
        with self._lock:
            closed = self._is_closed()
            entry = self._servers.get(server_key)
        if closed:
            raise tool_error(SERVICE_CLOSED_TEXT)
        if entry is None or _session_key_of(entry) != key:
            raise tool_error(SERVER_GONE_TEXT)
        return entry

    async def _aready_for_call(self, entry: ServerEntry, cfg: dict) -> None:
        """Шаги 2–3: дождаться своего подключения или начать его.

        Сервер без постоянной сессии при разрешающем правиле 30 с
        вызывается сразу. Ожидание — не дольше ``discovery_timeout_sec``
        настроек потребителя и снимается стопом хода.
        """
        key = _session_key_of(entry)
        with self._lock:
            connected = self._connected(key)
            attempting = self._attempting(key)
        if connected:
            return
        if not attempting:
            if not self.may_connect(entry):
                raise tool_error(self._refusal(entry))
            if not cfg.get("persistent_session"):
                return
        meta = settings_of(self._settings).mcp.meta
        ok = await await_attempt(
            self.start_connect(entry),
            timeout=meta.discovery_timeout_sec,
            server_key=entry.key,
        )
        # Попытку могли снять сверка (сервер ушёл) или закрытие.
        self._call_entry(entry.key, key)
        if not ok:
            raise tool_error(self._refusal(entry))

    def _refusal(self, entry: ServerEntry) -> str:
        with self._lock:
            own = self._failures.get(_session_key_of(entry))
        return refusal_text(own, shared_failure(entry))

    async def _arun_call(
        self,
        entry: ServerEntry,
        cfg: dict,
        call: Callable[[], Awaitable[Any]],
        timeout: float,
    ) -> Any:
        """Шаг 4: вызов дочерней задачей — её, а не ход, снимает закрытие
        потребителя. Между проверкой набора и созданием задачи ожиданий
        нет: закрытие, начатое позже, задачу уже увидит."""
        with use_owner(self.owner), use_call_slots(self._slots):
            task = asyncio.get_running_loop().create_task(call())
        with self._lock:
            self._inflight.add(task)
        task.add_done_callback(self._forget_call)
        try:
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                task.cancel()
                await self._aabandon_call(entry, cfg)
                raise
            return await self._call_outcome(entry, cfg, task, timeout)
        finally:
            self._leave_if_gone(entry)

    def _forget_call(self, task: asyncio.Task) -> None:
        with self._lock:
            self._inflight.discard(task)

    async def _aabandon_call(self, entry: ServerEntry, cfg: dict) -> None:
        """Шаг 6: брошенный без стопа вызов постоянного stdio закрывает
        сессию (поток иначе держал бы её замок до конца срока, §5.8)."""
        stop = current_user_stop()
        if stop is not None and stop.requested:
            return
        if not is_persistent_stdio(cfg):
            return
        self._drop_connection(_session_key_of(entry))
        await self._aclose_rest(entry)

    async def _call_outcome(
        self, entry: ServerEntry, cfg: dict, task: asyncio.Task, timeout: float
    ) -> Any:
        """Шаги 4–5, 7: исход вызова; ответ после закрытия — «сервис MCP
        остановлен»."""
        exc = None if task.cancelled() else task.exception()
        if task.cancelled() or self._is_closed():
            raise tool_error(SERVICE_CLOSED_TEXT) from exc
        if exc is not None:
            await self._araise_call_failure(entry, cfg, exc, timeout)
        key = _session_key_of(entry)
        with self._lock:
            self._failures.pop(key, None)
            target = (
                self._sessions if cfg.get("persistent_session") else self._ok
            )
            target.add(key)
        return task.result()

    async def _araise_call_failure(
        self, entry: ServerEntry, cfg: dict, exc: Exception, timeout: float
    ) -> NoReturn:
        """Срок — ошибка тула без смены статуса; транспортный сбой — отказ
        потребителя; остальное (ошибка тула сервера, аргументов, стоп)
        уходит как есть."""
        if is_timeout(exc):
            raise timeout_error(exc, timeout) from exc
        if not is_transport_failure(exc):
            raise exc
        raise await self._atransport_failure(entry, cfg, exc) from exc

    async def _atransport_failure(
        self, entry: ServerEntry, cfg: dict, exc: Exception
    ) -> Exception:
        """Шаг 7: ошибка тула по транспортному сбою вызова.

        Сессию закрыли сверка или сам потребитель — отказ не пишется. Иначе
        сессия потребителя закрывается, отказ — в запись и свой; сетевой
        сервер у потребителя «на старте» переподключается в фоне.
        """
        key = _session_key_of(entry)
        with self._lock:
            gone = self._servers.get(entry.key) is not entry
            lost = cfg.get("persistent_session") and key not in self._sessions
        if gone:
            return tool_error(SERVER_GONE_TEXT)
        if lost:
            return tool_error(SESSION_LOST_TEXT)
        self._drop_connection(key)
        await self._aclose_rest(entry)
        if self._is_closed():
            return tool_error(SERVICE_CLOSED_TEXT)
        failure = self._service.record_failure(entry, exc)
        logger.warning(
            "MCP %s: вызов потребителя %s потерял связь: %s",
            entry.key,
            self.label,
            failure.reason,
        )
        with self._lock:
            self._failures[key] = failure
        if self._retries(entry):
            self._start_attempt(entry)
        return tool_error(failure.reason)

    def _leave_if_gone(self, entry: ServerEntry) -> None:
        """Шаг 8: сервер ушёл из набора, пока шёл вызов, — закрыть сессию,
        которую вызов мог открыть заново (§5.3)."""
        with self._lock:
            gone = (
                not self.closed and self._servers.get(entry.key) is not entry
            )
        if gone:
            self._leave(entry)

    async def aclose(self) -> None:
        """Отказать в новой работе, снять попытки, серии и идущие вызовы,
        дождаться закрытия остатков, закрыть все сессии потребителя.

        Каждый вызов проходит весь путь: шаги идемпотентны, поэтому
        повторный вызов (в том числе после прерванного) доделывает
        остаток, а не выходит сразу.
        """
        self._service._bind_loop()
        with self._lock:
            self.closed = True
            work = [
                *self._connects.values(),
                *self._series.values(),
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
                        self._backend_cache = None
                        self._service._forget_consumer(self)


__all__ = ["RETRY_PAUSES_SEC", "McpConsumer"]
