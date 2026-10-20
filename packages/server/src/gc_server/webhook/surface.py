"""Поверхность webhook: задача внешней системы → прогон агента в процессе.

Пара ``(system, task_id)`` — тред; в каждый момент у неё не больше одного
прогона. Сообщение в идущий прогон ложится в его очередь (``queued``), пока
строится контекст — ждёт чужую сборку, иначе строит сам (``started``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Dict

from fastapi import FastAPI, HTTPException, Request
from gc_core.agent_edits.ctx import RUN_MODE_SERVER
from gc_core.agent_executor import AgentExecutor
from gc_core.config import cli_forces_legacy_model
from gc_core.config_modules.auth import build_auth_connector
from gc_core.run_context import acreate_run_context
from gc_core.thread_store import validate_thread_id
from gc_core.threads.facade import Threads
from gc_core.threads.models import ThreadKey, ThreadRecord
from gc_core.trace_id import normalize_trace_id
from gc_core.utils.generate_message_id import (
    generate_msg_id_sha256 as generate_message_id_func,
)

from gc_server.config import RESERVED_SYSTEMS
from gc_server.surface import (
    check_system_allowed,
    http_error,
    install_bearer,
    read_json_object,
)
from gc_server.webhook.janitor import start_threads_cleanup
from gc_server.webhook.registry import AgentRunRegistry
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
    check_system_allowed(system)
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
    check_system_allowed(sys_norm)
    return sys_norm, task_norm


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

    # --- жизненный цикл ----------------------------------------------------

    async def astart(self) -> None:
        cleanup = self.config.threads_cleanup
        if cleanup.enabled:
            from gc_core.thread_store import resolve_threads_dir

            start_threads_cleanup(
                enabled=True,
                interval_seconds=cleanup.interval_seconds,
                ttl_days=cleanup.ttl_days,
                threads_dir=resolve_threads_dir(self.settings),
                logger=logger,
            )

    async def astop(self) -> None:
        self.registry.cancel_all()

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
        # Бронь и прогон — одним снимком реестра, бронь первой: пока строится
        # контекст, нового прогона ещё нет, а под тем же ключом лежит прошлый
        # завершённый с TTL 15 минут.
        building_since, run = self.registry.get_status_snapshot(
            system=sys_norm, task_id=task_norm
        )
        base = {"system": sys_norm, "task_id": task_norm}
        if building_since is not None:
            return {
                "status": "running",
                **base,
                "thread_id": task_norm,
                "started_at": building_since,
                "last_activity_at": building_since,
            }
        if run is None:
            raise HTTPException(
                status_code=404, detail={"status": "not_found"}
            )
        return run_status_body(run, base)

    async def events_webhook(self, request: Request):
        payload = await read_json_object(request)
        task_id, trace_id = request_task_identifiers(payload)
        system = request_system(payload, self.config.default_system)
        message_str = json.dumps(payload, ensure_ascii=False)
        queued = await self._deliver_or_reserve(
            system, task_id, message_str, trace_id
        )
        if queued:
            return {
                "status": "queued",
                "payload": payload,
                "system": system,
                "task_id": task_id,
            }
        try:
            await self._start_run(system, task_id, message_str, trace_id)
        finally:
            self.registry.release_reservation(system=system, task_id=task_id)
        return {
            "status": "started",
            "payload": payload,
            "system": system,
            "task_id": task_id,
        }

    async def _deliver_or_reserve(
        self, system: str, task_id: str, message: str, trace_id: str | None
    ) -> bool:
        """``True`` — сообщение легло в идущий прогон; ``False`` — ключ
        забронирован за этим запросом, прогон строить ему.

        Путь проходится заново после каждой чужой сборки контекста: пока она
        шла, прогон мог стартовать — тогда сообщение уходит в него, — а мог и
        не стартовать, если сборка упала; тогда строим сами. Ждём не дольше
        ``webhook.context_build_wait_seconds`` в сумме за запрос.
        """
        deadline: float | None = None
        while True:
            if self.registry.add_user_message(
                system=system,
                task_id=task_id,
                make_message=_make_user_message,
                content=message,
            ):
                _log_ignored_trace_id(trace_id, system, task_id)
                return True
            if self.registry.reserve(system=system, task_id=task_id):
                return False
            loop = asyncio.get_running_loop()
            if deadline is None:
                deadline = loop.time() + self.config.context_build_wait_seconds
            await self._await_foreign_build(system, task_id, deadline)

    async def _await_foreign_build(
        self, system: str, task_id: str, deadline: float
    ) -> None:
        try:
            async with asyncio.timeout_at(deadline):
                await self.registry.await_build(system=system, task_id=task_id)
        except TimeoutError:
            ceiling = self.config.context_build_wait_seconds
            logger.warning(
                "context_build_wait_timeout system=%s task_id=%s ceiling_s=%g",
                system,
                task_id,
                ceiling,
                extra={
                    "event": "context_build_wait_timeout",
                    "system": system,
                    "task_id": task_id,
                    "ceiling_s": ceiling,
                },
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "status": "error",
                    "payload": "task context is still being built, retry later",
                },
            ) from None

    async def _start_run(
        self, system: str, task_id: str, message: str, trace_id: str | None
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
        await self.registry.astart_if_absent(
            system=system,
            task_id=task_id,
            executor_factory=AgentExecutor,
            ctx=ctx,
            initial_user_message=message,
            thread_id=task_id,
            verbose=None,
        )

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


def run_status_body(run: Any, base: Dict[str, Any]) -> Dict[str, Any]:
    """Статус завершённого или идущего прогона реестра."""
    body = {**base, "thread_id": run.thread_id, "started_at": run.started_at}
    if not run.done:
        return {
            "status": "running",
            **body,
            "last_activity_at": run.last_activity_at,
        }
    body["finished_at"] = run.finished_at
    if run.cancelled:
        # Снятый прогон — не упавший: остановка сервера, явная отмена.
        return {"status": "cancelled", **body}
    if run.error is not None:
        return {"status": "failed", **body, "error": repr(run.error)}
    return {"status": "completed", **body, "result": run.result}


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
