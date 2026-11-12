"""Потребитель сервиса MCP: сверка набора, срез тулов и исполнитель вызова.

Потребитель — то, чьи это сессии (окно TUI, агент ACP, прогон ``gc-cli``).
``McpConsumer`` — фасад: сверяет набор серверов со сборкой, закрепляет и
отдаёт срез тулов, закрывает потребителя. Соединения и всё состояние под
замком — у ``McpConnections`` (``consumer_connections``).

Вызов тула модели идёт через ``_acall`` (спека §5.4): он ждёт своё
подключение, идёт по сессии потребителя под его владельцем и слотами, а
потерю сессии, смерть сервера, срок и уход сервера из набора отдаёт ошибкой
тула; чистые части исполнителя — в ``consumer_call``.
"""

import asyncio
import logging
import traceback
from functools import partial
from typing import TYPE_CHECKING, Any, Awaitable, Callable, NoReturn

from gc_core.config_modules.field_meta import plain_dump
from gc_core.mcp_session_owner import new_owner_token, use_owner
from gc_core.settings import settings_of
from gc_core.tools.connect_mcp.abandonable import use_call_slots
from gc_core.tools.connect_mcp.agent_tools import mcp_cache_config
from gc_core.tools.connect_mcp.api import acall_mcp_tool
from gc_core.tools.connect_mcp.call_errors import first_leaf
from gc_core.tools.connect_mcp.consumer_call import (
    SERVER_GONE_TEXT,
    SESSION_LOST_TEXT,
    await_attempt,
    call_target,
    is_timeout,
    is_transport_family,
    lost_session,
    passes_through,
    refusal_text,
    stop_requested,
    timeout_error,
    tool_error,
)
from gc_core.tools.connect_mcp.consumer_connections import (
    RETRY_PAUSES_SEC,
    McpConnections,
    session_key_of,
)
from gc_core.tools.connect_mcp.consumer_status import (
    ServerStatus,
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
from gc_core.tools.connect_mcp.persistent_stdio import is_persistent_stdio
from gc_core.tools.connect_mcp.registry import ToolsCache
from gc_core.tools.connect_mcp.service_records import (
    SERVICE_CLOSED_TEXT,
    McpServiceClosed,
    ServerEntry,
)
from gc_core.user_stop import UserStopInterrupt

if TYPE_CHECKING:
    from gc_core.tools.connect_mcp.service import McpService

logger = logging.getLogger(__name__)


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
    """Потребитель сервиса: сверка набора, срез тулов, закрытие.

    ``_connections`` — соединения и всё состояние под замком.
    ``_pinned`` — закреплённый срез ``{ключ: (запись, статус)}``;
    ``_backend_cache`` — кэш тулов серверов, исполняемых внутри бэкенда
    сборки (§5.10), пока бэкенд сборки — тот же объект. Оба меняются
    только на цикле сервиса.
    """

    def __init__(self, service: "McpService", label: str) -> None:
        self._service = service
        self._connections = McpConnections(
            service, new_owner_token("mcp"), label
        )
        self._pinned: dict[str, tuple[ServerEntry, ServerStatus]] = {}
        self._backend_cache: ToolsCache | None = None

    @property
    def owner(self) -> str:
        """Владелец сессий потребителя."""
        return self._connections.owner

    @property
    def closed(self) -> bool:
        """Потребитель закрыт (``aclose``)."""
        return self._connections.closed

    def next_build(self) -> int:
        """Номер новой сборки; набор меняет только сверка самой поздней."""
        return self._connections.next_build()

    def servers(self) -> dict[str, ServerEntry]:
        """Набор: записи серверов последней начатой сборки."""
        return self._connections.servers()

    def status_of(self, entry: ServerEntry) -> ServerStatus:
        """Статус сервера у этого потребителя (спека §5.5)."""
        return self._connections.status_of(entry)

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
                self._connections.wrapper_cfg(entry),
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
        # Быстрый путь: устаревшая сборка не трогает записи и каталог.
        # Решающая проверка — в ``replace_set``, под замком замены.
        if self._connections.is_stale(build):
            return
        timeout = settings_of(settings).mcp.meta.discovery_timeout_sec
        entries = {
            key: self._service.entry(key, cfg, discovery_timeout=timeout)
            for key, cfg in _agent_side_definitions(
                settings, os_backend
            ).items()
        }
        gone = self._connections.replace_set(build, settings, entries)
        if gone is None:
            return
        for entry in gone:
            self._connections.leave(entry)
        self.pin()
        waits: set[asyncio.Task] = set()
        for entry in entries.values():
            task = self._connect_for_build(entry)
            if task is not None:
                waits.add(task)
        await self._await_build(waits, timeout)
        self._refuse_if_closed()
        # Обогнанная сборка не закрепляет срез: иначе вход в ход не увидел
        # бы изменения, случившегося после возврата новой сборки.
        if self._connections.is_latest(build):
            self.pin()

    def _refuse_if_closed(self) -> None:
        if self._connections.refused():
            raise McpServiceClosed(SERVICE_CLOSED_TEXT)

    def _connect_for_build(self, entry: ServerEntry) -> asyncio.Task | None:
        """Начать подключение по политике хоста; отдать задачу, если
        сборке её ждать."""
        connected, task = self._connections.attempt_of(entry)
        if connected:
            return None
        wanted = self._service.connect_on_start or not entry.tools_known
        if task is None and wanted and self._connections.may_connect(entry):
            task = self._connections.start_attempt(entry)
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

    def _call_entry(self, server_key: str, key: str) -> ServerEntry:
        """Запись сервера вызова в наборе; иначе — ошибка тула."""
        closed = self._connections.refused()
        entry = self._connections.current(server_key)
        if closed:
            raise tool_error(SERVICE_CLOSED_TEXT)
        if entry is None or session_key_of(entry) != key:
            raise tool_error(SERVER_GONE_TEXT)
        return entry

    async def _aready_for_call(self, entry: ServerEntry, cfg: dict) -> None:
        """Шаги 2–3: дождаться своего подключения или начать его.

        Сервер без постоянной сессии при разрешающем правиле 30 с
        вызывается сразу. Ожидание — не дольше ``discovery_timeout_sec``
        настроек потребителя и снимается стопом хода.
        """
        key = session_key_of(entry)
        timeout = self._connections.discovery_timeout()
        deadline = asyncio.get_running_loop().time() + timeout
        while self._needs_connect(entry, cfg):
            await await_attempt(
                self._connections.start_connect(entry),
                deadline=deadline,
                timeout=timeout,
                server_key=entry.key,
            )
            # Попытку могли снять закрытие или сверка: сервер ушёл —
            # ошибка; его вернули — следующий круг ждёт новую попытку.
            self._call_entry(entry.key, key)

    def _needs_connect(self, entry: ServerEntry, cfg: dict) -> bool:
        """Вызову нужно дождаться подключения; правило 30 с не пускает —
        ошибка тула с причиной. После неудачной попытки её отказ свежий,
        поэтому круг ожиданий кончается этой ошибкой."""
        connected, attempt = self._connections.attempt_of(entry)
        if connected:
            return False
        if attempt is not None:
            return True
        if not self._connections.may_connect(entry):
            raise tool_error(self._refusal(entry))
        return bool(cfg.get("persistent_session"))

    def _refusal(self, entry: ServerEntry) -> str:
        own = self._connections.own_failure(entry)
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
        connections = self._connections
        with use_owner(connections.owner), use_call_slots(connections.slots):
            task = asyncio.get_running_loop().create_task(call())
        generation = connections.begin_call(task, entry)
        task.add_done_callback(self._forget_call)
        try:
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                task.cancel()
                await self._aabandon_call(entry, cfg)
                raise
            return await self._call_outcome(
                entry, cfg, task, timeout, generation
            )
        finally:
            connections.leave_if_gone(entry)

    def _forget_call(self, task: asyncio.Task) -> None:
        """Снять вызов с учёта и забрать его исключение: после отмены
        вызывающего исход задачи никто не ждёт."""
        self._connections.end_call(task)
        if not task.cancelled():
            task.exception()

    async def _aabandon_call(self, entry: ServerEntry, cfg: dict) -> None:
        """Шаг 6: брошенный без стопа вызов постоянного stdio закрывает
        сессию (поток иначе держал бы её замок до конца срока, §5.8)."""
        if stop_requested() or not is_persistent_stdio(cfg):
            return
        await self._connections.adrop(entry)

    async def _call_outcome(
        self,
        entry: ServerEntry,
        cfg: dict,
        task: asyncio.Task,
        timeout: float,
        generation: int,
    ) -> Any:
        """Шаги 4–5, 7: исход вызова; ответ после закрытия — «сервис MCP
        остановлен». Задачу, отменённую не закрытием (её снял транспорт),
        — как потерю соединения."""
        if self._connections.refused():
            if not task.cancelled():
                task.exception()
            raise tool_error(SERVICE_CLOSED_TEXT)
        if task.cancelled():
            raise await self._atransport_failure(
                entry, cfg, lost_session(), generation
            )
        exc = task.exception()
        if exc is not None:
            await self._araise_call_failure(
                entry, cfg, exc, timeout, generation
            )
        self._connections.confirm(entry, cfg, generation)
        return task.result()

    async def _araise_call_failure(
        self,
        entry: ServerEntry,
        cfg: dict,
        exc: BaseException,
        timeout: float,
        generation: int,
    ) -> NoReturn:
        """По первому листу группы: срок — ошибка тула без смены статуса;
        ошибка тула сервера, аргументов, стоп и durable — как есть; отмена
        под запрошенным стопом — стоп; всё остальное — отказ связи с
        сервером."""
        leaf = first_leaf(exc)
        if is_timeout(leaf):
            raise timeout_error(leaf, timeout) from exc
        if passes_through(leaf):
            raise leaf
        cause = exc
        if isinstance(leaf, asyncio.CancelledError):
            # Под запрошенным стопом — стоп вызова, начатого до него (как
            # голая отмена в ``_mcp_call_under_stop``); иначе — снятие
            # вызова транспортом, как отменённая задача: ход вызывающего
            # она не отменяет.
            if stop_requested():
                raise UserStopInterrupt(kind="mcp", started=True) from exc
            cause = lost_session()
        raise await self._atransport_failure(
            entry, cfg, cause, generation
        ) from exc

    async def _atransport_failure(
        self,
        entry: ServerEntry,
        cfg: dict,
        exc: BaseException,
        generation: int,
    ) -> Exception:
        """Шаг 7: ошибка тула по отказу связи у вызова.

        ``generation`` — поколение соединения на старте вызова. Сервер ушёл
        из набора — «сервер отключён»; поколение постоянной сессии сменилось
        (её закрыл сам потребитель) — «сессия сервера закрыта во время
        вызова»; отказ в обоих случаях не пишется. Иначе — общий шаг отказа
        соединений (``McpConnections.afail``) и запись в журнал.
        """
        gone, lost = self._connections.call_lost(entry, cfg, generation)
        if gone:
            return tool_error(SERVER_GONE_TEXT)
        if lost:
            return tool_error(SESSION_LOST_TEXT)
        failure = await self._connections.afail(entry, exc)
        if failure is None:
            return tool_error(SERVICE_CLOSED_TEXT)
        self._log_call_failure(entry, exc, failure.reason)
        return tool_error(failure.reason)

    def _log_call_failure(
        self, entry: ServerEntry, exc: BaseException, reason: str
    ) -> None:
        """Отказ связи — предупреждение; лист вне семейств транспорта
        (скорее ошибка нашего кода) — ошибка с кадрами трассировки. Тексты
        исключений в лог не идут: в них бывает адрес со строкой запроса."""
        leaf = first_leaf(exc)
        name = type(leaf).__name__
        label = self._connections.label
        if is_transport_family(leaf):
            logger.warning(
                "MCP %s: вызов потребителя %s потерял связь (%s): %s",
                entry.key,
                label,
                name,
                reason,
            )
            return
        logger.error(
            "MCP %s: вызов потребителя %s упал (%s): %s\n%s",
            entry.key,
            label,
            name,
            reason,
            "".join(traceback.format_tb(leaf.__traceback__)),
        )

    async def aclose(self) -> None:
        """Отказать в новой работе, снять попытки, серии и идущие вызовы,
        дождаться закрытия остатков, закрыть все сессии потребителя; в
        конце хвоста — сбросить кэш бэкенда и сняться с учёта сервиса.

        Каждый вызов проходит весь путь: шаги идемпотентны, поэтому
        повторный вызов (в том числе после прерванного) доделывает
        остаток, а не выходит сразу.
        """
        await self._connections.aclose(self._forget)

    def _forget(self) -> None:
        self._backend_cache = None
        self._service._forget_consumer(self)


__all__ = ["RETRY_PAUSES_SEC", "McpConsumer"]
