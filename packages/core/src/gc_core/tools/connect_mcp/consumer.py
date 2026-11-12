"""Потребитель сервиса MCP: сверка набора, срез тулов, закрытие.

Потребитель — то, чьи это сессии (окно TUI, агент ACP, прогон ``gc-cli``).
``McpConsumer`` — фасад: сверяет набор серверов со сборкой, закрепляет и
отдаёт срез тулов, закрывает потребителя. Соединения и всё состояние под
замком — у ``McpConnections`` (``consumer_connections``); вызов тула модели
— у ``McpCallExecutor`` (``consumer_executor``), его ``acall`` срез отдаёт
обёрткам тулов.
"""

import asyncio
import gc
from typing import TYPE_CHECKING, Any

from gc_core.backends.protocol import base_backend
from gc_core.config_modules.field_meta import plain_dump
from gc_core.mcp_session_owner import new_owner_token
from gc_core.settings import settings_of
from gc_core.tools.connect_mcp.agent_tools import mcp_cache_config
from gc_core.tools.connect_mcp.consumer_connections import (
    RETRY_PAUSES_SEC,
    McpConnections,
)
from gc_core.tools.connect_mcp.consumer_executor import McpCallExecutor
from gc_core.tools.connect_mcp.consumer_status import ServerStatus
from gc_core.tools.connect_mcp.consumer_view import (
    McpToolsView,
    aadd_backend_part,
    add_server,
    backend_config,
    callers_pins,
)
from gc_core.tools.connect_mcp.effective import effective_mcp_servers
from gc_core.tools.connect_mcp.execute_on import runs_in_agent_runtime
from gc_core.tools.connect_mcp.registry import ToolsCache
from gc_core.tools.connect_mcp.service_records import (
    SERVICE_CLOSED_TEXT,
    McpServiceClosed,
    ServerEntry,
)
from gc_core.trace_id import trace_id_from_config

if TYPE_CHECKING:
    from gc_core.tools.connect_mcp.service import McpService


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

    ``_connections`` — соединения и всё состояние под замком;
    ``_executor`` — исполнитель вызова по этим соединениям.
    ``_pinned`` — закреплённый срез ``{ключ: (запись, статус)}``;
    ``_pin_stale`` — набор контекста не пересобран по нему (``reset_pin``);
    ``_backend_cache`` — кэш тулов серверов, исполняемых внутри бэкенда
    сборки (§5.10), пока исходный бэкенд сборки (под обёрткой записи
    правок) — тот же объект; ``_trace_id`` — trace последней сверенной
    сборки (её настройки строят определения подключений и обёрток).
    Все четыре меняются только на цикле сервиса.
    """

    def __init__(self, service: "McpService", label: str) -> None:
        self._service = service
        self._connections = McpConnections(
            service, new_owner_token("mcp"), label
        )
        self._executor = McpCallExecutor(self._connections)
        self._pinned: dict[str, tuple[ServerEntry, ServerStatus]] = {}
        self._pin_stale = False
        self._backend_cache: ToolsCache | None = None
        self._trace_id = ""

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
        время проверки и номер повтора на пересборку не влияют. После
        ``reset_pin`` — изменился в любом случае.
        """
        pinned = {
            key: (entry, self.status_of(entry))
            for key, entry in self.servers().items()
        }
        previous, self._pinned = self._pinned, pinned
        stale, self._pin_stale = self._pin_stale, False
        return stale or _pin_view(previous) != _pin_view(pinned)

    def reset_pin(self) -> None:
        """Набор контекста не пересобран по закреплённому срезу (сбой
        пересборки): следующее закрепление сообщит об изменении. Сам срез
        остаётся — его читают сборки субагентов."""
        self._pin_stale = True

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
        view = McpToolsView({}, {}, {}, self._executor.acall)
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
        """Кэш бэкенда сервиса, если он есть; иначе свой — с исходным
        бэкендом сборки, новый, когда исходный бэкенд — другой объект.

        Сравнивается бэкенд под обёрткой записи правок: сборка контекста,
        пересборка на входе в ход и сборка субагента получают один бэкенд
        в разных обёртках, и обнаружение не должно повторяться.
        """
        shared = self._service.backend_tools_cache()
        if shared is not None:
            return shared
        backend = base_backend(os_backend)
        cache = self._backend_cache
        if cache is None or cache.os_backend is not backend:
            cache = self._backend_cache = ToolsCache(os_backend=backend)
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
        self._trace_id = trace_id_from_config(settings)
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

    async def astart_turn(self) -> bool:
        """Вход в ход (спека §5.5–§5.6); ничего не ждёт.

        По каждому серверу набора назначает фоновую работу (попытку
        подключения, перечитывание списка тулов), затем закрепляет срез и
        возвращает, изменился ли он с прошлого закрепления. Закрытый
        потребитель или остановленный сервис — ``False``: набор
        контекста остаётся прежним.
        """
        if self._connections.refused():
            return False
        self._service._bind_loop()
        for entry in self.servers().values():
            self._connections.start_turn(entry)
        return self.pin()

    async def aend_turn(self) -> None:
        """Уборка конца хода — порт ``mcp_cleanup`` контекста (спека §5.2).

        Сессии потребителя живут дольше хода. Исключение — серверы с
        ``trace_header_enabled``, когда у сборки есть trace: он зашит в
        сессию, поэтому их подключения снимаются и закрываются, а следующий
        ход подключает их заново по обычным правилам (§5.9). Снятие — не
        отказ: правило 30 с не срабатывает, идущий вызов получает «сессия
        закрыта во время вызова». Идущая попытка подключения не трогается.
        Затем сборка циклического мусора, как в прежней уборке хода.
        Закрытый потребитель ничего не делает.
        """
        if self.closed:
            return
        if self._trace_id:
            await asyncio.gather(
                *(
                    self._connections.adrop(entry)
                    for entry in self._trace_sessions()
                )
            )
        gc.collect()

    def _trace_sessions(self) -> list[ServerEntry]:
        """Подключённые серверы набора, которым разрешён trace.

        Идущая попытка не в счёт: закрытие сессии посреди подключения
        сделало бы его отказом, а с ним — и правило 30 с.
        """
        return [
            entry
            for entry in self.servers().values()
            if entry.cfg.get("trace_header_enabled")
            and self._connections.attempt_of(entry)[0]
        ]

    async def aclose(self) -> None:
        """Отказать в новой работе, снять попытки, серии, перечитывания
        списка и идущие вызовы, дождаться закрытия остатков, закрыть все
        сессии потребителя; в конце хвоста — сбросить кэш бэкенда и
        сняться с учёта сервиса.

        Каждый вызов проходит весь путь: шаги идемпотентны, поэтому
        повторный вызов (в том числе после прерванного) доделывает
        остаток, а не выходит сразу.
        """
        await self._connections.aclose(self._forget)

    def _forget(self) -> None:
        self._backend_cache = None
        self._service._forget_consumer(self)


__all__ = ["RETRY_PAUSES_SEC", "McpConsumer"]
