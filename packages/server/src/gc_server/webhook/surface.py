"""Поверхность webhook: задача внешней системы → прогон агента в процессе.

Пара ``(system, task_id)`` — тред; в каждый момент у него не больше одного
хода — на любом инстансе (аренда треда в хранилище). Сообщение сначала
ложится во входящий поток треда. Тред уже ведут (свой прогон, своя сборка
контекста или чужая живая аренда) — ``queued``: ход заберёт сообщение сам.
Иначе этот запрос строит прогон (``started``). Статус задачи — в хранилище.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Dict

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder as jsonable
from gc_core.agent_edits.ctx import RUN_MODE_SERVER
from gc_core.agent_executor import AgentExecutor
from gc_core.config import cli_forces_legacy_model
from gc_core.config_modules.auth import build_auth_connector
from gc_core.execution.environment import utc_now
from gc_core.run_context import acreate_run_context
from gc_core.thread_store import validate_thread_id
from gc_core.threads.errors import OwnershipError
from gc_core.threads.facade import Threads
from gc_core.threads.instance import instance_id
from gc_core.threads.models import ThreadKey, ThreadQuery, ThreadRecord
from gc_core.threads.rules import list_cursor_of
from gc_core.trace_id import normalize_trace_id
from gc_core.utils.generate_message_id import (
    generate_msg_id_sha256 as generate_message_id_func,
)

from gc_server.config import RESERVED_SYSTEMS
from gc_server.surface import (
    allowed_system,
    http_error,
    install_bearer,
)
from gc_server.webhook import status as task_status
from gc_server.webhook.callbacks import (
    CallbackSender,
    allowed_callback,
    make_event,
)
from gc_server.webhook.history_page import (
    HistoryPage,
    Position,
    ahistory_page,
)
from gc_server.webhook.registry import ActiveRun, AgentRunRegistry
from gc_server.webhook.request import ensure_task_id, normalize_system

if TYPE_CHECKING:
    from gc_server.app import ServerRuntime

logger = logging.getLogger(__name__)

_ERR_SYSTEM_AND_TASK_ID = "system and task_id are required"
#: Прогон восстановления: адрес обратного вызова задачи остаётся прежним.
KEEP_CALLBACK = object()
#: Как часто забывать квитанции старше окна идемпотентности.
_RECEIPTS_PRUNE_SECONDS = 3600


def webhook_settings(runtime: ServerRuntime, name: str = "webhook"):
    """Настройки агента вебхука: за ним нет человека, слова — запрос
    внешней системы, режим запуска — сервер."""
    return runtime.surface_settings(name).with_runtime(
        run_mode=RUN_MODE_SERVER,
        human_channel=False,
        external_user_words=True,
    )


def request_task_identifiers(payload: dict) -> tuple[str, str | None]:
    """``task_id`` (присланный или новый) и ``trace_id``; негодные — 400."""
    try:
        task_id = ensure_task_id(payload)
    except ValueError as e:
        # Присланный task_id уезжает в имя каталога и дальше в пути ФС.
        # Отвергаем ДО любого побочного эффекта, а не падаем позже в сторе.
        raise http_error(400, f"invalid task_id: {e}") from None
    # trace_id — поле контекста задачи, а не часть запроса к модели: снимаем его
    # ДО json.dumps(payload), иначе значение уедет в промпт модели.
    try:
        trace_id = normalize_trace_id(payload.pop("trace_id", None))
    except ValueError as exc:
        raise http_error(400, f"invalid trace_id: {exc}") from None
    return task_id, trace_id


def request_system(payload: dict, default_system: str) -> str:
    """``system`` из тела или умолчание поверхности; чужая поверхность — 400."""
    try:
        system = normalize_system(
            payload.get("system"), default_system=default_system
        )
    except ValueError as exc:
        raise http_error(400, str(exc)) from None
    system = allowed_system(system)
    payload["system"] = system
    return system


def path_key(system: str, task_id: str) -> tuple[str, str]:
    """``(system, task_id)`` из пути ``/runs`` и ``/logs``; негодные — 400."""
    sys_norm = str(system or "").lower().strip()
    task_norm = str(task_id or "").strip()
    if not sys_norm or not task_norm:
        raise http_error(400, _ERR_SYSTEM_AND_TASK_ID)
    try:
        task_norm = validate_thread_id(task_norm)
    except ValueError as e:
        raise http_error(400, str(e)) from None
    return allowed_system(sys_norm), task_norm


def _log_ignored_trace_id(trace_id, system, task_id):
    if trace_id:
        # Единственное место, где trace может быть легитимно потерян: запрос
        # подмешан в живой ран, чьи ctx, логгер, имя лог-файла и конфиг
        # MCP-серверов уже зафиксированы. Поэтому у потери есть событие.
        logger.warning(
            "trace_id_ignored: trace_id=%s system=%s task_id=%s",
            trace_id,
            system,
            task_id,
            extra={
                "event": "trace_id_ignored",
                "system": system,
                "task_id": task_id,
                "trace_id": trace_id,
            },
        )


class WebhookSurface:
    """Ручки ``/events/webhook``, ``/runs/…``, ``/logs/…``, ``/healthcheck``."""

    name = "webhook"

    def __init__(self, runtime: ServerRuntime) -> None:
        self.runtime = runtime
        self.config = runtime.config.webhook
        self.section = runtime.config.surface("webhook")
        self.settings = webhook_settings(runtime)
        self.registry = AgentRunRegistry()
        self._pruner: asyncio.Task | None = None
        self.recovery_task: asyncio.Task | None = None
        self._receipts: asyncio.Task | None = None
        self.callbacks = CallbackSender(self.config.callbacks)

    # --- жизненный цикл ----------------------------------------------------

    async def astart(self) -> None:
        self._receipts = asyncio.create_task(self._areceipts_loop())
        if self.config.recovery.enabled:
            from gc_server.webhook.recovery import arecover_forever

            self.recovery_task = asyncio.create_task(arecover_forever(self))
        cleanup = self.config.threads_cleanup
        if not cleanup.enabled:
            return
        # Чистка — через фасад и только задачи вебхука: на файлах каталог
        # тредов по умолчанию общий с gc-cli и TUI.
        store = self.threads().store
        logger.info(
            "чистка тредов включена (%s): ttl %g дн., интервал %d с",
            getattr(store, "threads_dir", "") or "хранилище storage.threads",
            cleanup.ttl_days,
            cleanup.interval_seconds,
        )
        self._pruner = asyncio.create_task(self._aprune_loop())

    async def astop(self) -> None:
        # Снятые прогоны пишут статус cancelled — дождаться их до закрытия
        # хранилища процессом.
        tasks = self.registry.cancel_all()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.callbacks.aclose()
        background = [
            t for t in (self._pruner, self.recovery_task, self._receipts) if t
        ]
        self._pruner = self.recovery_task = self._receipts = None
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)

    async def aprune_once(self, *, older_than: timedelta) -> int:
        """Удалить задачи вебхука (тред со статусом вебхука), не менявшиеся
        дольше ``older_than``; треды других поверхностей, интерактивных
        сессий и тред под бессрочной арендой (durable-исполнитель) не
        трогаются."""
        cutoff = (utc_now() - older_than).isoformat()
        threads = self.threads()
        stale = [
            record.key
            async for record in threads.iter_threads(updated_before=cutoff)
            if record.key.system not in RESERVED_SYSTEMS
            and task_status.FRONTEND in record.frontend
        ]
        removed = 0
        for key in stale:
            try:
                await threads.delete(key)
            except OwnershipError:
                continue
            removed += 1
        return removed

    async def _aprune_loop(self) -> None:
        cleanup = self.config.threads_cleanup
        while True:
            try:
                removed = await self.aprune_once(
                    older_than=timedelta(days=cleanup.ttl_days)
                )
                if removed:
                    logger.info("чистка тредов: удалено %d", removed)
            except Exception:
                logger.warning("чистка тредов не удалась", exc_info=True)
            await asyncio.sleep(cleanup.interval_seconds)

    # --- приложение -------------------------------------------------------

    def app(self) -> FastAPI:
        from gc_server.chat.http_guard import install_http_guard
        from gc_server.webhook.api_v1 import build_v1_routes

        app = FastAPI()
        install_bearer(app, self.section.auth_token)
        build_v1_routes(app, self)
        # POST — только JSON (браузер не шлёт такой без preflight: CSRF со
        # страницы в браузере пользователя), на loopback-бинде Host — только
        # loopback-имя (DNS rebinding).
        install_http_guard(
            app,
            cors_origins=[],
            bind_host=str(self.section.host),
            max_body_bytes=self.section.max_body_bytes,
        )
        app.add_api_route("/healthcheck", healthcheck, methods=["GET"])
        return app

    @property
    def default_system(self) -> str:
        return self.config.default_system

    @property
    def signing(self) -> Any:
        """Подпись тела запросов — секция webhook (общая с swarm)."""
        return self.runtime.config.webhook

    @property
    def events_poll_seconds(self) -> float:
        from gc_core.settings import settings_of

        return settings_of(self.settings).thread_store.inbox_poll_seconds

    def threads(self) -> Threads:
        """Треды вебхука — через фасад, там же, куда пишет его ход."""
        return self.runtime.threads("webhook")

    # --- ручки ------------------------------------------------------------

    async def astatus(self, key: ThreadKey) -> Dict[str, Any] | None:
        """Статус задачи (``None`` — задачи нет) и сколько сообщений ждёт
        доставки во входящем потоке."""
        base = {"system": key.system, "task_id": key.thread}
        record = await self.threads().get(key)
        if _foreign(record):
            return None
        pending = {"pending_messages": _pending(record)}
        # Контекст задачи строит этот процесс: прогона ещё нет, а статус в
        # хранилище — прошлого прогона.
        building_since = self.registry.building_since(
            system=key.system, task_id=key.thread
        )
        run = self.registry.run_of(system=key.system, task_id=key.thread)
        status = task_status.status_of(record)
        since = building_since
        if (
            since is None
            and run is not None
            and (status or {}).get("run_id") != run.notes.get("run_id")
        ):
            # Свой прогон ещё не взял тред (статус пишет взявший).
            since = run.started_at
        if since is not None:
            return {
                "status": "running",
                **base,
                "thread_id": key.thread,
                "started_at": since,
                "last_activity_at": since,
                **pending,
            }
        if status is None:
            return None
        return {**task_status.status_body(status, record, base), **pending}

    async def asubmit(
        self,
        key: ThreadKey,
        message: Dict[str, Any],
        *,
        op_id: str | None,
        trace_id: str | None,
        callback_url: str | None = None,
    ) -> Dict[str, Any]:
        """Сообщение — во входящий поток треда; тред уже ведут — ``queued``,
        иначе этот запрос строит прогон (``started``)."""
        if callback_url is not None:
            await self._check_callback(key, callback_url)
        if _foreign(await self.threads().get(key)):
            raise http_error(
                409,
                "task belongs to the swarm surface",
                system=key.system,
                task_id=key.thread,
            )
        receipt = await self.threads().submit(
            key, message, op_id=op_id, source="webhook"
        )
        seq = receipt.max_seq.get("inbox")
        if op_id is not None and await self._delivered(key, seq):
            # Повтор по ключу идемпотентности: сообщение уже доставлено —
            # нового хода не нужно.
            return {"status": "duplicate", "message_seq": seq}
        if not await self._take_start(key.system, key.thread):
            _log_ignored_trace_id(trace_id, key.system, key.thread)
            return {"status": "queued", "message_seq": seq}
        try:
            await self._start_run(
                key.system, key.thread, trace_id, callback_url=callback_url
            )
        finally:
            self.registry.release_reservation(
                system=key.system, task_id=key.thread
            )
        return {"status": "started", "message_seq": seq}

    async def _check_callback(self, key: ThreadKey, url: str) -> None:
        """``callback_url`` — только под allowed_urls. Адрес задаёт запрос,
        запускающий прогон (его пишет взявший тред ход): в идущую задачу —
        только тот же адрес, иначе 409 до любой записи — итог принадлежит
        тому, кто задачу поставил."""
        if not allowed_callback(url, self.config.callbacks.allowed_urls):
            raise http_error(
                400, "callback_url is not under webhook.callbacks.allowed_urls"
            )
        record = await self.threads().get(key)
        status = task_status.status_of(record) or {}
        busy = (
            self.registry.is_running(system=key.system, task_id=key.thread)
            or self.registry.building_since(
                system=key.system, task_id=key.thread
            )
            is not None
            or await self.threads().busy_owner(key) is not None
            or status.get("status") in ("running", "interrupted")
        )
        if busy and status.get("callback_url") != url:
            raise http_error(
                409,
                "callback_url is set by the request that starts the task",
            )

    async def _delivered(self, key: ThreadKey, seq: int | None) -> bool:
        record = await self.threads().get(key)
        return (
            bool(seq)
            and record is not None
            and (record.state.inbox_seq >= seq)
        )

    async def aprune_receipts(self, *, older_than: timedelta) -> int:
        """Забыть квитанции старше окна идемпотентности."""
        return await self.threads().prune(older_than)

    async def amaintain_once(self) -> None:
        """Обслуживание хранилища тредов: квитанции старше окна
        идемпотентности (и надгробия) и — у хранилища, которое умеет
        (Redis, S3), — сироты прерванных записей (``sweep``)."""
        window = timedelta(hours=self.config.idempotency_ttl_hours)
        await self.aprune_receipts(older_than=window)
        sweep = getattr(self.threads().store, "sweep", None)
        if callable(sweep):
            await sweep()

    async def _areceipts_loop(self) -> None:
        while True:
            try:
                await self.amaintain_once()
            except Exception:
                logger.warning(
                    "обслуживание хранилища тредов не удалось", exc_info=True
                )
            await asyncio.sleep(_RECEIPTS_PRUNE_SECONDS)

    async def acancel(self, key: ThreadKey) -> Dict[str, Any] | None:
        """Отменить ход задачи с любого инстанса: поток ``control`` треда
        (свой прогон — ещё и сразу)."""
        from gc_server.webhook.api_v1 import FINAL

        status = await self.astatus(key)
        if status is None:
            return None
        if status["status"] in FINAL:
            return {"status": status["status"]}
        self.registry.stop(system=key.system, task_id=key.thread)
        await self.threads().request_cancel(key)
        return {"status": "cancelling"}

    async def alist(
        self, *, system: str | None, limit: int, cursor: str | None
    ) -> tuple[list[Dict[str, Any]], str | None]:
        """Задачи страницей в ``limit``: треды других поверхностей
        отсеиваются, список дочитывается, пока страница не полна."""
        store = self.threads().store
        tasks: list[Dict[str, Any]] = []
        while True:
            page = await store.list_threads(
                ThreadQuery(system=system, limit=limit, cursor=cursor)
            )
            taken = [r for r in page.items if _is_task(r)]
            taken = taken[: limit - len(tasks)]
            tasks += [_task_entry(record) for record in taken]
            if len(tasks) == limit:
                last = taken[-1]
                more = last is not page.items[-1] or page.next_cursor
                return tasks, list_cursor_of(last) if more else None
            if page.next_cursor is None:
                return tasks, None
            cursor = page.next_cursor

    async def amessages(
        self, key: ThreadKey, *, position: Position | None, limit: int
    ) -> HistoryPage | None:
        threads = self.threads()
        record = await threads.get(key)
        if record is None or _foreign(record):
            return None
        return await ahistory_page(threads.store, record, position, limit)

    async def _take_start(self, system: str, task_id: str) -> bool:
        """Строить ли прогон этому запросу. Нет — тред уже ведут: свой
        прогон или сборка (ход заберёт сообщение опросом или в начале) либо
        другой инстанс (его аренда жива)."""
        if self.registry.is_running(system=system, task_id=task_id):
            return False
        owner = await self.threads().busy_owner(
            ThreadKey(system=system, thread=task_id)
        )
        if owner is not None:
            return False
        return self.registry.reserve(system=system, task_id=task_id)

    async def astart_recovered(
        self,
        system: str,
        task_id: str,
        *,
        prepare: Callable[[], Awaitable[bool]] | None = None,
    ) -> bool:
        """Продолжить бесхозную задачу (восстановление после падения):
        ``False`` — её уже ведёт или строит этот процесс, сборка упала или
        ``prepare`` отказался. ``prepare`` идёт под бронью этого процесса:
        записи о прерванном ходе не попадут в задачу, которую он же ведёт."""
        if not self.registry.reserve(system=system, task_id=task_id):
            return False
        try:
            if prepare is not None and not await prepare():
                return False
            await self._start_run(system, task_id, None)
        except Exception:
            logger.warning(
                "recovery: прогон %s/%s не собран",
                system,
                task_id,
                exc_info=True,
            )
            return False
        finally:
            self.registry.release_reservation(system=system, task_id=task_id)
        return True

    async def _start_run(
        self,
        system: str,
        task_id: str,
        trace_id: str | None,
        *,
        callback_url: Any = KEEP_CALLBACK,
    ) -> None:
        loaded = (
            self.settings.with_runtime(trace_id=trace_id)
            if trace_id
            else self.settings
        )
        force_legacy = cli_forces_legacy_model(loaded)
        if force_legacy:
            logger.warning(
                "agent_overrides: уровни отключены на этот запуск (легаси-форс)"
            )
        ctx = await acreate_run_context(
            loaded,
            auth_connector=build_auth_connector(loaded),
            force_legacy=force_legacy,
            **self.runtime.store_kwargs(),
        )
        run_id = f"{instance_id()}:{uuid.uuid4().hex[:12]}"
        started_at = time.time()
        # Статус «running» пишет только ход, взявший тред: прогон, чей тред
        # перехватил другой инстанс, статус не трогает.
        ctx.on_thread_claimed = functools.partial(
            self._amark_running, run_id, started_at, callback_url
        )
        await self.registry.astart(
            system=system,
            task_id=task_id,
            executor=AgentExecutor(ctx),
            ctx=ctx,
            on_finish=self._amark_finished,
            notes={"run_id": run_id},
        )

    async def _amark_running(
        self,
        run_id: str,
        started_at: float,
        callback_url: Any,
        system: str,
        thread_id: str,
    ) -> None:
        """Ход взял тред: статус running и адрес обратного вызова прогона
        (запрос без адреса стирает адрес прошлого прогона; восстановление
        сохраняет адрес задачи)."""
        fields: Dict[str, Any] = {
            "status": "running",
            "run_id": run_id,
            "instance": instance_id(),
            "started_at": started_at,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        if callback_url is not KEEP_CALLBACK:
            fields["callback_url"] = callback_url
        await task_status.aset_status(
            self.threads(),
            ThreadKey(system=system, thread=thread_id),
            **fields,
        )

    async def _amark_finished(self, run: ActiveRun) -> None:
        """Итог прогона — в статус задачи, если статус всё ещё его: тред
        мог уже взять следующий прогон (на любом инстансе)."""
        if run.ending == "handed_off" and run.result is None:
            return  # тред так и не взяли — статус ведёт взявший
        if run.ending == "interrupted":
            return  # остановка сервера: задачу продолжит восстановление
        key = ThreadKey(system=run.system, thread=run.task_id)
        threads = self.threads()
        record = await threads.get(key)
        current = task_status.status_of(record)
        if current is None or current.get("run_id") != run.notes["run_id"]:
            return
        final = _final_fields(run)
        final["recovery_attempts"] = 0
        if final["status"] == "failed" and record is not None:
            # Задача сдалась: входящие до этой границы восстановление не
            # поднимает (иначе упавший ход повторялся бы по кругу).
            final["inbox_upto"] = record.max_seq.get("inbox", 0)
        await task_status.aset_status(threads, key, **final)
        self.notify(key, {**current, **final})

    def notify(self, key: ThreadKey, status: Dict[str, Any]) -> None:
        """Событие о смене статуса задачи — обратным вызовом, если он
        настроен (``webhook.callbacks.url`` или ``callback_url`` задачи)."""
        url = status.get("callback_url")
        if url and not allowed_callback(
            url, self.config.callbacks.allowed_urls
        ):
            # Список сменили после приёма запроса: адреса в нём больше нет.
            logger.warning("callback: %s больше не в allowed_urls", url)
            return
        url = url or self.config.callbacks.url
        if url:
            self.callbacks.send(
                url, make_event(status["status"], key, jsonable(status))
            )


def _final_fields(run: ActiveRun) -> Dict[str, Any]:
    """Поля статуса завершённого прогона."""
    fields: Dict[str, Any] = {"finished_at": time.time()}
    if run.ending == "cancelled":
        # Снятый прогон — не упавший: остановка сервера, явная отмена.
        return {"status": "cancelled", **fields}
    if run.ending == "failed":
        return {"status": "failed", **fields, "error": repr(run.error)}
    return {"status": "completed", **fields, "result": run.result}


def make_user_message(content: str) -> Dict[str, Any]:
    return {
        "role": "user",
        "content": content,
        "id": generate_message_id_func("m"),
    }


def _task_entry(record: ThreadRecord) -> dict:
    status = task_status.status_of(record) or {}
    return {
        "system": record.key.system,
        "task_id": record.key.thread,
        "status": status.get("status"),
        "updated_at": record.info.updated_at or record.info.created_at,
    }


def _is_task(record: ThreadRecord) -> bool:
    """Тред — задача вебхука (не интерактивная сессия, не swarm)."""
    return record.key.system not in RESERVED_SYSTEMS and not _foreign(record)


def _foreign(record: ThreadRecord | None) -> bool:
    """Тред задачи swarm в общей базе — его ведёт песочница, а не вебхук."""
    return record is not None and "swarm" in record.frontend


def _pending(record: ThreadRecord | None) -> int:
    """Сообщения входящего потока, ещё не доставленные в историю."""
    if record is None:
        return 0
    return max(0, record.max_seq.get("inbox", 0) - record.state.inbox_seq)


async def healthcheck():
    return {"status": "ok"}
