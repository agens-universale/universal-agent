"""Сервис MCP хоста: общие записи серверов и каталог тулов.

Хост создаёт сервис и закрывает его; сервис не синглтон. Запись сервера —
пара «ключ, отпечаток» и общие для потребителей факты о нём: сырой список
тулов (из каталога или от последнего удачного подключения), время проверки
и последний отказ. Сам сервис в сеть не ходит и статусов не держит:
подключается потребитель, статус у каждого потребителя свой, а исход
подключения обновляет запись через ``record_success`` / ``record_failure``.

Сервис работает на одном цикле событий: он привязывается к циклу первого
обращения, обращение с другого цикла — ошибка. Закрывает сервис хост
(``aclose``): сервис перестаёт принимать работу, закрывает всех своих
потребителей в общий бюджет и добивает по журналу процессов то, что не
уложилось. Аварийный выход — ``kill_processes_now``: синхронно, без цикла и
замков, из любого потока (спека §5.12).
"""

import asyncio
import contextlib
import contextvars
import logging
import os
import threading
import time
from functools import partial
from typing import Any, Awaitable, Callable

from gc_core.tools.connect_mcp.catalog import read_catalog, write_catalog
from gc_core.tools.connect_mcp.client import McpProtocolError
from gc_core.tools.connect_mcp.consumer import McpConsumer
from gc_core.tools.connect_mcp.consumer_status import ServerStatus
from gc_core.tools.connect_mcp.failure_reason import (
    classify_failure,
    is_server_gone,
)
from gc_core.tools.connect_mcp.process_journal import (
    ProcessJournal,
    sweep_orphans,
)
from gc_core.tools.connect_mcp.registry import (
    ToolsCache,
    build_checked_entries,
)
from gc_core.tools.connect_mcp.server_identity import (
    connection_fingerprint,
    is_process_transport,
    with_service_defaults,
)
from gc_core.tools.connect_mcp.service_records import (
    SERVICE_CLOSED_TEXT,
    Failure,
    McpServiceClosed,
    ServerEntry,
)
from gc_core.tools.connect_mcp.transport import (
    LAUNCHED_COMMAND_FIELD,
    _resolve_command,
)

logger = logging.getLogger(__name__)

#: Отсрочка после срока бюджета закрытия сервиса: снятые сроком закрытия
#: потребителей ждутся не дольше неё. Снятие прерывает ожидание попыток,
#: вызовов и закрытий; хвост, начатый отменой (закрытие сессий во всех
#: реестрах, у постоянного stdio — до 7 с на сессию плюс close-тул), идёт
#: дальше без новых отмен, и что не уложилось в отсрочку, доигрывает в
#: фоне. Худший возврат ``aclose`` — бюджет плюс отсрочка, от числа
#: серверов и сессий не зависит.
CLOSE_GRACE_SEC = 3.0
#: Бюджет ``aclose`` по умолчанию: в него укладывается close-тул сервера.
CLOSE_BUDGET_SEC = 10.0
#: Гашение по журналу: сколько ждать после SIGTERM группам до SIGKILL.
KILL_WAIT_SEC = 2.0
#: Сбой ``exec`` пускателя — исключение, которым кончился бы запуск без
#: пускателя; прочие сбои — ``OSError``. Причина отказа та же, что без него.
_LAUNCH_ERRORS = {"command_not_found": FileNotFoundError}


def _launches_process(server_cfg: dict) -> bool:
    """Процесс сервера с постоянной сессией на POSIX — запускается через
    пускатель и учитывается в журнале (спека §5.12).

    Постоянный stdio и SDK порождают такой процесс в новой сессии, поэтому
    группа процесса равна его pid и групповой сигнал по записи журнала его
    берёт. Разовый stdio (процесс в группе хоста) и Windows — без пускателя.
    """
    return (
        os.name == "posix"
        and is_process_transport(server_cfg)
        and bool(server_cfg.get("persistent_session"))
        and bool(server_cfg.get("command"))
    )


def _launch_args(server_cfg: dict) -> list[str]:
    """Аргументы команды — как их готовит запуск без пускателя."""
    args = server_cfg.get("args") or []
    if not isinstance(args, list):
        args = [args]
    return [str(a) for a in args]


def _warn_bad_catalog(server_key: str, cfg: dict, tools: list) -> None:
    """Каталог, записанный до проверки списка, с негодным описанием тула —
    предупреждение один раз на запись сервера. Отказ сервера в срезе даёт
    ``server_offer`` (там повтор — на отладочном уровне)."""
    try:
        build_checked_entries(server_key, cfg, tools, quiet=True)
    except McpProtocolError as exc:
        logger.warning(
            "MCP %s: каталог тулов негоден, тулы не предлагаются: %s",
            server_key,
            classify_failure(exc, cfg)[1],
        )


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


def _kill_and_remove(journal: ProcessJournal) -> None:
    journal.kill_all(wait_sec=KILL_WAIT_SEC)
    journal.remove()


async def _asweep_orphans() -> None:
    """Уборка сирот в рабочем потоке; сбой — в лог (§5.12, линия 3)."""
    try:
        swept = await asyncio.to_thread(sweep_orphans)
    except Exception as exc:
        logger.warning(
            "Сервис MCP: уборка сирот не удалась: %s: %s",
            type(exc).__name__,
            exc,
        )
        return
    if swept:
        logger.info("Сервис MCP: погашено групп сирот: %s", swept)


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
    ``aclose``. ``_journal`` — журнал процессов серверов (§5.12): объект
    есть сразу, каталог заводится при первой подстановке пускателя, у
    сервиса без процессных серверов его нет. ``_sweep_task`` — уборка
    сирот, начатая первой сверкой.
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
        self._journal = ProcessJournal()
        self._sweep_task: asyncio.Task | None = None

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

    def matching_status(
        self, consumer: McpConsumer, settings: Any
    ) -> dict[str, ServerStatus]:
        """Статусы серверов набора, чьё определение совпадает с ``settings``;
        с любого потока. Для отчёта до сверки набора: изменённый сервер под
        тем же ключом ждёт своего статуса (спека §5.7)."""
        return consumer.statuses_matching(settings)

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

    def with_launcher(self, server_key: str, server_cfg: dict) -> dict:
        """Определение для запуска: процесс сервера с постоянной сессией
        на POSIX — копия, где команда сервера идёт через пускатель журнала
        сервиса; иначе ``server_cfg`` как есть. Звать с цикла сервиса.

        Команда раскрывается так же, как при запуске без пускателя (``~``,
        mcp-env); поиск по PATH пускатель делает в окружении сервера, а
        ``cwd`` и ``env`` наследует без изменений. Окружение строится по
        команде сервера (``LAUNCHED_COMMAND_FIELD``), а не по пускателю.
        Отпечаток и ключ сессии считаются по определению до подстановки.
        """
        if not _launches_process(server_cfg):
            return server_cfg
        server_command = _resolve_command(str(server_cfg["command"]))
        command, args = self._journal.launch_argv(
            server_command, _launch_args(server_cfg), server_key
        )
        return {
            **server_cfg,
            "command": command,
            "args": args,
            LAUNCHED_COMMAND_FIELD: server_command,
        }

    async def aprune_journal(self) -> None:
        """Снять из журнала записи вышедших процессов — в рабочем потоке
        (на macOS метка процесса читается через ``ps``). Каталога журнала
        нет — ничего, и поток не заводится."""
        if self._journal.directory is not None:
            await asyncio.to_thread(self._journal.prune)

    def _begin_orphan_sweep(self) -> None:
        """Первая сверка сервиса: уборка сирот мёртвых хостов в фоне
        (§5.12, линия 3). Звать с цикла сервиса."""
        if self._sweep_task is None:
            self._sweep_task = asyncio.get_running_loop().create_task(
                _asweep_orphans(), context=contextvars.Context()
            )

    def kill_processes_now(self) -> None:
        """Аварийное гашение (спека §5.12, линия 1); с любого потока, из
        обработчика сигнала, синхронно.

        Сначала запрет: вызовы, сверки и новые подключения получают
        «сервис MCP остановлен» (иначе идущий ход поднял бы сервер
        заново). Цикл сервиса, если он жив, снимет фоновую работу и
        идущие вызовы потребителей, когда получит управление: из чужого
        потока — раньше, чем увидит смерть серверов, из обработчика
        сигнала на потоке цикла — после возврата гашения. Затем по
        журналу — SIGTERM группам, до ``KILL_WAIT_SEC`` ожидания, SIGKILL
        каждой; журнал читается ещё раз. Замков сервиса и потребителей не
        берёт, цикла не ждёт; повторный вызов и вызов одновременно с
        ``aclose`` безопасны. Сессии потребителей не закрывает — их
        закрывает ``aclose``.
        """
        self._stopped = True
        loop = self._loop
        if loop is not None:
            # Цикл уже закрыт — будить и снимать на нём некого.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._halt)
        self._journal.kill_all(wait_sec=KILL_WAIT_SEC)

    def _halt(self) -> None:
        """На цикле сервиса после аварийного гашения: разбудить ждущих
        остановку и снять работу потребителей — повтор вызова или попытка
        после гашения подняли бы сервер заново."""
        self._stopped_event.set()
        with self._lock:
            consumers = list(self._consumers)
        for consumer in consumers:
            consumer.halt_work()

    def _launch_failure(
        self, entry: ServerEntry, exc: BaseException
    ) -> OSError | None:
        """Отказ «сервер завершился, не ответив», а последний запуск
        сервера не дошёл до его команды (сбой ``exec`` пускателя), —
        исключение, которым кончился бы запуск без пускателя; иначе
        ``None``. Срок, протокол и ответы сервера журнал не читают."""
        if not _launches_process(entry.cfg):
            return None
        if not is_server_gone(exc):
            return None
        record = self._journal.launch_error(entry.key)
        if record is None:
            return None
        error = _LAUNCH_ERRORS.get(record.error, OSError)
        return error(record.detail or record.error)

    def _forget_consumer(self, consumer: McpConsumer) -> None:
        """Закрытый потребитель больше не числится за сервисом."""
        with self._lock:
            self._consumers.discard(consumer)

    async def aclose(self, budget: float = CLOSE_BUDGET_SEC) -> None:
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
        По сроку бюджета — предупреждение в лог (не исключение),
        незакрытые потребители снимаются, а их хвост закрытия сессий
        доигрывает в фоне. Сразу после закрытия потребителей (в срок или
        по отсрочке) оставшиеся процессы журнала гасятся, как при
        аварийном гашении, и каталог журнала удаляется; уборка сирот
        ждётся в остаток бюджета. Возврат — не позже ``budget +
        CLOSE_GRACE_SEC + KILL_WAIT_SEC``; хосту не нужно знать, истёк ли
        бюджет: добивать после возврата нечего. Задача закрытия живёт,
        пока живёт цикл сервиса: закрыть сервис надо до закрытия цикла.
        """
        with self._lock:
            self._bind_loop()
            self._stopped = True
            task = self._close_task
            if task is None:
                # Пустой контекст: стоп хода и владелец сессий вызывающего
                # в закрытие не попадают — владельца ставит потребитель.
                task = asyncio.get_running_loop().create_task(
                    self._aclose_all(budget),
                    context=contextvars.Context(),
                )
                self._close_task = task
        self._stopped_event.set()
        await asyncio.shield(task)

    async def _aclose_all(self, budget: float) -> None:
        """Задача закрытия: потребители в бюджет, добивание по журналу,
        уборка сирот — в остаток бюджета.

        Отмена посреди закрытия потребителей (свёртка цикла: отмена
        оставшихся задач при остановке цикла агента, выход
        ``asyncio.run``) добивает по журналу синхронно, на потоке цикла:
        ждать рабочий поток на сворачиваемом цикле уже нельзя, а блок
        цикла до ``KILL_WAIT_SEC`` — только на этом пути. Отмена остаётся
        отменой.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        try:
            await self._aclose_consumers(budget)
        except BaseException:
            _kill_and_remove(self._journal)
            raise
        await self._aclose_journal()
        sweep = self._sweep_task
        if sweep is not None:
            await asyncio.wait(
                {sweep}, timeout=max(0.0, deadline - loop.time())
            )

    async def _aclose_journal(self) -> None:
        """Погасить процессы журнала, оставшиеся после закрытия
        потребителей, и удалить журнал (в рабочем потоке).

        Журнал читается заново: брошенный поток попытки или вызова мог
        запустить процесс уже после закрытия сессий. Удалённый журнал
        каталога больше не заводит — и подстановка пускателя после
        закрытия (срез закрытого потребителя) тоже. Каталога не было —
        рабочий поток не нужен.
        """
        if self._journal.directory is None:
            self._journal.remove()
            return
        await asyncio.to_thread(_kill_and_remove, self._journal)

    async def _aclose_consumers(self, budget: float) -> None:
        """Закрыть потребителей, числящихся за сервисом, в общий бюджет.

        Остановка уже стоит под замком, поэтому снимок полон: нового
        потребителя после неё не открыть. По сроку — предупреждение,
        незакрытые снимаются один раз и ждутся не дольше
        ``CLOSE_GRACE_SEC``. Исход каждого закрытия — в лог (у
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
            _, pending = await asyncio.wait(pending, timeout=CLOSE_GRACE_SEC)
        for task, consumer in tasks.items():
            if task in pending:
                task.add_done_callback(partial(_report_close, consumer))
            else:
                _report_close(consumer, task)

    def entry(self, server_key: str, server_cfg: dict) -> ServerEntry:
        """Запись сервера по ``(ключ, отпечаток)``; новая берёт тулы из
        каталога, если он есть. ``server_cfg`` — определение без trace."""
        self._bind_loop()
        fingerprint = connection_fingerprint(server_cfg)
        found = self._entries.get((server_key, fingerprint))
        if found is not None:
            return found
        cfg = with_service_defaults(server_cfg)
        created = ServerEntry(
            key=server_key,
            cfg=cfg,
            fingerprint=fingerprint,
        )
        cataloged = read_catalog(server_key, cfg)
        if cataloged is not None:
            _warn_bad_catalog(server_key, cfg, cataloged)
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
        """Отказ подключения; тулы записи остаются.

        Сервер завершился, не ответив, потому что его процесс не
        запустился (сбой ``exec`` пускателя), — причина из журнала:
        транспорт видит только сервер, вышедший до ответа.
        """
        self._bind_loop()
        launch = self._launch_failure(entry, exc)
        cause = exc if launch is None else launch
        failure = Failure(self._clock(), *classify_failure(cause, entry.cfg))
        entry.last_failure = failure
        return failure


__all__ = [
    "CLOSE_BUDGET_SEC",
    "CLOSE_GRACE_SEC",
    "KILL_WAIT_SEC",
    "McpService",
]
