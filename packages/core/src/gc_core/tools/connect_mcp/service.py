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
from gc_core.tools.connect_mcp.process_journal import ProcessJournal
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
from gc_core.tools.connect_mcp.transport import _resolve_command

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
    ``aclose``. ``_journal`` — журнал процессов серверов (§5.12);
    заводится при первой подстановке пускателя.
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
        self._journal: ProcessJournal | None = None

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

    def with_launcher(self, server_key: str, server_cfg: dict) -> dict:
        """Определение для запуска: процесс сервера с постоянной сессией
        на POSIX — копия, где команда сервера идёт через пускатель журнала
        сервиса; иначе ``server_cfg`` как есть. Звать с цикла сервиса.

        Команда раскрывается так же, как при запуске без пускателя (``~``,
        mcp-env); поиск по PATH пускатель делает в окружении сервера, а
        ``cwd`` и ``env`` наследует без изменений. Отпечаток и ключ сессии
        считаются по определению до подстановки.
        """
        if not _launches_process(server_cfg):
            return server_cfg
        if self._journal is None:
            self._journal = ProcessJournal()
        command, args = self._journal.launch_argv(
            _resolve_command(str(server_cfg["command"])),
            _launch_args(server_cfg),
            server_key,
        )
        return {**server_cfg, "command": command, "args": args}

    async def aprune_journal(self) -> None:
        """Снять из журнала записи вышедших процессов — в рабочем потоке
        (на macOS метка процесса читается через ``ps``). Журнала нет —
        ничего."""
        journal = self._journal
        if journal is not None:
            await asyncio.to_thread(journal.prune)

    def _launch_failure(
        self, entry: ServerEntry, exc: BaseException
    ) -> OSError | None:
        """Отказ «сервер завершился, не ответив», а последний запуск
        сервера не дошёл до его команды (сбой ``exec`` пускателя), —
        исключение, которым кончился бы запуск без пускателя; иначе
        ``None``. Срок, протокол и ответы сервера журнал не читают."""
        if self._journal is None or not _launches_process(entry.cfg):
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
        Возврат — не позже ``budget + CLOSE_GRACE_SEC``: по сроку бюджета
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


__all__ = ["CLOSE_BUDGET_SEC", "CLOSE_GRACE_SEC", "McpService"]
