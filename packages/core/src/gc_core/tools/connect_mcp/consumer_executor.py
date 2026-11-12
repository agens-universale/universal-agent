"""Исполнитель вызова тула потребителя сервиса MCP (спека §5.4, §5.8).

``McpCallExecutor`` своего состояния не держит: учёт вызовов, поколения
соединений, подтверждение и отказ связи — у ``McpConnections``. Вызов ждёт
своё подключение (или начинает его по правилу 30 с), идёт по сессии
потребителя под его владельцем и слотами, а потерю сессии, смерть сервера,
срок и уход сервера из набора отдаёт ошибкой тула; чистые части — в
``consumer_call``.
"""

import asyncio
import logging
import traceback
from functools import partial
from typing import Any, Awaitable, Callable, NoReturn

from gc_core.mcp_session_owner import use_owner
from gc_core.tools.connect_mcp.abandonable import use_call_slots
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
    McpConnections,
    session_key_of,
)
from gc_core.tools.connect_mcp.consumer_status import shared_failure
from gc_core.tools.connect_mcp.persistent_stdio import is_persistent_stdio
from gc_core.tools.connect_mcp.service_records import (
    SERVICE_CLOSED_TEXT,
    McpServiceClosed,
    ServerEntry,
)
from gc_core.user_stop import UserStopInterrupt

logger = logging.getLogger(__name__)


class McpCallExecutor:
    """Исполнитель вызова тулов одного потребителя по его соединениям."""

    def __init__(self, connections: McpConnections) -> None:
        self._connections = connections

    async def acall(
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
                self._start_connect(entry),
                deadline=deadline,
                timeout=timeout,
                server_key=entry.key,
            )
            # Попытку могли снять закрытие или сверка: сервер ушёл —
            # ошибка; его вернули — следующий круг ждёт новую попытку.
            self._call_entry(entry.key, key)

    def _start_connect(self, entry: ServerEntry) -> asyncio.Future:
        """Ожидание своей попытки; сервис остановили из другого потока
        после проверки набора — ошибка тула «сервис MCP остановлен»."""
        try:
            return self._connections.start_connect(entry)
        except McpServiceClosed:
            raise tool_error(SERVICE_CLOSED_TEXT) from None

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
        остановлен». Снятый закрытием или гашением вызов постоянного
        stdio закрывает сессию, как брошенный (шаг 6): иначе его поток
        мог бы поднять процесс в ещё открытую сессию. Задачу, отменённую
        не закрытием (её снял транспорт), — как потерю соединения."""
        if self._connections.refused():
            if not task.cancelled():
                task.exception()
            elif is_persistent_stdio(cfg):
                await self._connections.adrop(entry)
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


__all__ = ["McpCallExecutor"]
