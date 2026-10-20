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
import json
import logging
import time
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Dict

from fastapi import FastAPI, HTTPException, Request
from gc_core.agent_edits.ctx import RUN_MODE_SERVER
from gc_core.agent_executor import AgentExecutor
from gc_core.config import cli_forces_legacy_model
from gc_core.config_modules.auth import build_auth_connector
from gc_core.execution.environment import utc_now
from gc_core.run_context import acreate_run_context
from gc_core.thread_store import validate_thread_id
from gc_core.threads.facade import Threads
from gc_core.threads.instance import instance_id
from gc_core.threads.models import ThreadKey, ThreadRecord
from gc_core.trace_id import normalize_trace_id
from gc_core.utils.generate_message_id import (
    generate_msg_id_sha256 as generate_message_id_func,
)

from gc_server.config import RESERVED_SYSTEMS
from gc_server.surface import (
    allowed_system,
    http_error,
    install_bearer,
    read_json_object,
)
from gc_server.webhook import status as task_status
from gc_server.webhook.janitor import start_threads_cleanup
from gc_server.webhook.registry import ActiveRun, AgentRunRegistry
from gc_server.webhook.request import ensure_task_id, normalize_system

if TYPE_CHECKING:
    from gc_server.app import ServerRuntime

logger = logging.getLogger(__name__)

_ERR_SYSTEM_AND_TASK_ID = "system and task_id are required"


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

    # --- жизненный цикл ----------------------------------------------------

    async def astart(self) -> None:
        if self.config.recovery.enabled:
            from gc_server.webhook.recovery import arecover_forever

            self.recovery_task = asyncio.create_task(arecover_forever(self))
        cleanup = self.config.threads_cleanup
        if not cleanup.enabled:
            return
        if "thread_store" in self.runtime.store_kwargs():
            # Треды в базе: чистка — через фасад и только треды вебхука.
            logger.info(
                "чистка тредов включена (хранилище storage.threads): ttl %g "
                "дн., интервал %d с",
                cleanup.ttl_days,
                cleanup.interval_seconds,
            )
            self._pruner = asyncio.create_task(self._aprune_loop())
            return
        from gc_core.thread_store import resolve_threads_dir

        start_threads_cleanup(
            enabled=True,
            interval_seconds=cleanup.interval_seconds,
            ttl_days=cleanup.ttl_days,
            threads_dir=resolve_threads_dir(self.settings),
            logger=logger,
        )

    async def astop(self) -> None:
        # Снятые прогоны пишут статус cancelled — дождаться их до закрытия
        # хранилища процессом.
        tasks = self.registry.cancel_all()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        background = [t for t in (self._pruner, self.recovery_task) if t]
        self._pruner = self.recovery_task = None
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)

    async def aprune_once(self, *, older_than: timedelta) -> int:
        """Удалить треды вебхука, не менявшиеся дольше ``older_than``;
        треды других поверхностей и интерактивных сессий не трогаются."""
        cutoff = (utc_now() - older_than).isoformat()
        threads = self.threads()
        stale = [
            record.key
            async for record in threads.iter_threads(updated_before=cutoff)
            if record.key.system not in RESERVED_SYSTEMS
        ]
        for key in stale:
            await threads.delete(key)
        return len(stale)

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
        app = FastAPI()
        install_bearer(app, self.section.auth_token)
        app.add_api_route(
            "/events/webhook", self.events_webhook, methods=["POST"]
        )
        app.add_api_route(
            "/runs/{system}/{task_id}", self.get_run_status, methods=["GET"]
        )
        app.add_api_route(
            "/logs/all", self.get_all_thread_logs, methods=["GET"]
        )
        app.add_api_route(
            "/logs/{system}/{task_id}", self.get_thread_logs, methods=["GET"]
        )
        app.add_api_route("/healthcheck", healthcheck, methods=["GET"])
        return app

    def threads(self) -> Threads:
        """Треды сервера — через фасад."""
        return self.runtime.threads()

    # --- ручки ------------------------------------------------------------

    async def get_run_status(self, system: str, task_id: str):
        sys_norm, task_norm = path_key(system, task_id)
        base = {"system": sys_norm, "task_id": task_norm}
        # Контекст задачи строит этот процесс: прогона ещё нет, а статус в
        # хранилище — прошлого прогона.
        building_since = self.registry.building_since(
            system=sys_norm, task_id=task_norm
        )
        if building_since is not None:
            return {
                "status": "running",
                **base,
                "thread_id": task_norm,
                "started_at": building_since,
                "last_activity_at": building_since,
            }
        record = await self.threads().get(
            ThreadKey(system=sys_norm, thread=task_norm)
        )
        status = task_status.status_of(record)
        run = self.registry.run_of(system=sys_norm, task_id=task_norm)
        if run is not None and (status or {}).get("run_id") != run.notes.get(
            "run_id"
        ):
            # Свой прогон ещё не взял тред (статус пишет взявший): в
            # хранилище — прошлый итог или ничего.
            return {
                "status": "running",
                **base,
                "thread_id": task_norm,
                "started_at": run.started_at,
                "last_activity_at": run.started_at,
            }
        if status is None:
            raise HTTPException(
                status_code=404, detail={"status": "not_found"}
            )
        return task_status.status_body(status, record, base)

    async def events_webhook(self, request: Request):
        payload = await read_json_object(request)
        task_id, trace_id = request_task_identifiers(payload)
        system = request_system(payload, self.config.default_system)
        message = _make_user_message(json.dumps(payload, ensure_ascii=False))
        await self.threads().submit(
            ThreadKey(system=system, thread=task_id), message, source="webhook"
        )
        reply = {"payload": payload, "system": system, "task_id": task_id}
        if not await self._take_start(system, task_id):
            _log_ignored_trace_id(trace_id, system, task_id)
            return {"status": "queued", **reply}
        try:
            await self._start_run(system, task_id, trace_id)
        finally:
            self.registry.release_reservation(system=system, task_id=task_id)
        return {"status": "started", **reply}

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

    async def astart_recovered(self, system: str, task_id: str) -> bool:
        """Продолжить бесхозную задачу (восстановление после падения):
        ``False`` — её уже ведёт или строит этот процесс, или сборка упала."""
        if not self.registry.reserve(system=system, task_id=task_id):
            return False
        try:
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
        self, system: str, task_id: str, trace_id: str | None
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
            self._amark_running, run_id, started_at
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
        self, run_id: str, started_at: float, system: str, thread_id: str
    ) -> None:
        await task_status.aset_status(
            self.threads(),
            ThreadKey(system=system, thread=thread_id),
            status="running",
            run_id=run_id,
            instance=instance_id(),
            started_at=started_at,
            finished_at=None,
            result=None,
            error=None,
        )

    async def _amark_finished(self, run: ActiveRun) -> None:
        """Итог прогона — в статус задачи, если статус всё ещё его: тред
        мог уже взять следующий прогон (на любом инстансе)."""
        if run.ending == "handed_off" and run.result is None:
            return  # тред так и не взяли — статус ведёт взявший
        key = ThreadKey(system=run.system, thread=run.task_id)
        threads = self.threads()
        current = task_status.status_of(await threads.get(key))
        if current is None or current.get("run_id") != run.notes["run_id"]:
            return
        await task_status.aset_status(threads, key, **_final_fields(run))

    async def get_thread_logs(self, system: str, task_id: str):
        """Логи по ``(system, task_id)``: meta треда в прежней форме и
        сообщения истории."""
        sys_norm, task_norm = path_key(system, task_id)
        threads = self.threads()
        key = ThreadKey(system=sys_norm, thread=task_norm)
        record = await threads.get(key)
        if record is None:
            raise HTTPException(
                status_code=404, detail={"status": "not_found"}
            )
        return {
            "status": "ok",
            "system": sys_norm,
            "task_id": task_norm,
            "meta": threads.legacy_meta(record),
            "messages": [entry.data for entry in await threads.history(key)],
        }

    async def get_all_thread_logs(self):
        """Список тредов (system + task_id), свежие первыми."""
        threads = self.threads()
        out = [
            _thread_log_entry(r)
            async for r in threads.iter_threads()
            if r.key.system not in RESERVED_SYSTEMS
        ]
        out.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
        return {"status": "ok", "logs": out}


def _final_fields(run: ActiveRun) -> Dict[str, Any]:
    """Поля статуса завершённого прогона."""
    fields: Dict[str, Any] = {"finished_at": time.time()}
    if run.ending == "cancelled":
        # Снятый прогон — не упавший: остановка сервера, явная отмена.
        return {"status": "cancelled", **fields}
    if run.ending == "failed":
        return {"status": "failed", **fields, "error": repr(run.error)}
    return {"status": "completed", **fields, "result": run.result}


def _make_user_message(content: str) -> Dict[str, Any]:
    return {
        "role": "user",
        "content": content,
        "id": generate_message_id_func("m"),
    }


def _thread_log_entry(record: ThreadRecord) -> dict:
    system, task_id = record.key.system, record.key.thread
    return {
        "system": system,
        "task_id": task_id,
        "updated_at": record.info.updated_at or record.info.created_at,
        "messages_count": record.info.messages_count,
        "logs_url": f"/logs/{system}/{task_id}",
    }


async def healthcheck():
    return {"status": "ok"}
