"""Поверхность swarm: задача исполняется ``gc-cli`` в песочнице.

Интерфейс — как у вебхука: те же пути, тела, ответы и статусы; отличается
только исполнение. Задача — процесс песочницы ``swarm_<system>_<task_id>``;
второе сообщение в идущую задачу — отказ «занято» (409).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from typing import TYPE_CHECKING, Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from gc_core.thread_store import sanitize_system_id

from gc_server.surface import http_error, install_bearer, read_json_object
from gc_server.swarm import lifecycle as swarm_lifecycle
from gc_server.swarm import runner as swarm_runner
from gc_server.swarm import storage as swarm_storage
from gc_server.swarm.config import build_swarm_config
from gc_server.swarm.monitor import SwarmMonitor
from gc_server.webhook.janitor import start_swarm_cleanup
from gc_server.webhook.surface import (
    healthcheck,
    path_key,
    request_system,
    request_task_identifiers,
)

if TYPE_CHECKING:
    from gc_server.app import ServerRuntime

logger = logging.getLogger(__name__)

#: Статус песочницы → общий статус задачи (как у вебхука).
_COMMON_STATUS = {
    "queued": "running",
    "running": "running",
    "completed": "completed",
    "failed": "failed",
    "timeout": "failed",
    "unknown_failed": "failed",
}
_ACTIVE = frozenset({"queued", "running"})
_KILL_GRACE_S = 3.0


def swarm_id(system: str, task_id: str) -> str:
    return f"swarm_{system}_{task_id}"


class SwarmSurface:
    name = "swarm"

    def __init__(
        self, runtime: ServerRuntime, *, swarm: Dict[str, Any] | None = None
    ) -> None:
        self.runtime = runtime
        self.config = runtime.config
        self.section = runtime.config.surface("swarm")
        self.swarm = (
            swarm
            if swarm is not None
            else build_swarm_config(runtime.config.swarm_section())
        )
        self.monitor: SwarmMonitor | None = None
        # Чтение meta → решение «отказ или старт» → запись meta и Popen —
        # одним куском: два одинаковых запроса не запускают две песочницы.
        self._start_lock = threading.Lock()

    @property
    def threads_dir(self) -> str:
        return str(self.swarm["threads_dir"])

    @property
    def sandbox_root_dir(self) -> str:
        return str(self.swarm.get("sandbox_root_dir", ""))

    # --- жизненный цикл ----------------------------------------------------

    async def astart(self) -> None:
        monitor = SwarmMonitor(
            self.threads_dir,
            self.sandbox_root_dir,
            swarm_logs_dir=str(self.swarm.get("swarm_logs_dir", "")),
        )
        monitor.start_in_background()
        self.monitor = monitor
        start_swarm_cleanup(self.swarm, logger)

    async def astop(self) -> None:
        """Остановка сервера: SIGTERM группам живых песочниц, пауза, затем
        SIGKILL оставшимся; meta — ``unknown_failed`` / ``server_shutdown``.
        Без этого живые процессы становятся сиротами."""
        monitor = self.monitor
        if monitor is None:
            return
        active_ids = list(monitor.get_active_ids_copy())
        if active_ids:
            logger.info(
                "shutdown: terminating %d active sworm(s)", len(active_ids)
            )
            killed = self._terminate_groups(active_ids)
            await asyncio.sleep(_KILL_GRACE_S)
            for sid, pid in killed:
                if swarm_lifecycle._is_pid_alive(pid):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
                self._mark_shutdown(sid)
                monitor.unregister_active(sid)
        monitor.stop()
        self.monitor = None

    def _terminate_groups(
        self, active_ids: list[str]
    ) -> list[tuple[str, int]]:
        killed = []
        for sid in active_ids:
            meta = swarm_storage.load_swarm_meta(self.threads_dir, sid)
            pid = ((meta or {}).get("swarm_meta") or {}).get("pid")
            if isinstance(pid, int) and swarm_lifecycle._is_pid_alive(pid):
                try:
                    os.killpg(pid, signal.SIGTERM)
                    killed.append((sid, pid))
                except OSError:
                    pass
        return killed

    def _mark_shutdown(self, sid: str) -> None:
        try:
            existing = swarm_storage.load_swarm_meta(self.threads_dir, sid)
            if not existing:
                return
            status = _status_of(existing)
            if status in swarm_lifecycle.FINAL_STATUSES:
                return
            swarm_storage.save_swarm_meta(
                self.threads_dir,
                sid,
                base_meta={
                    "system": existing.get("system"),
                    "task_id": existing.get("task_id"),
                },
                swarm_meta_patch={
                    "status": "unknown_failed",
                    "finished_at": swarm_storage._utc_now_iso(),
                    "error_type": "server_shutdown",
                    "error_message": "Killed during server shutdown",
                    "sandbox_status": "completed",
                },
            )
        except Exception:
            logger.exception("shutdown: failed to update meta for %s", sid)

    # --- приложение -------------------------------------------------------

    def app(self) -> FastAPI:
        app = FastAPI()
        install_bearer(app, self.section.auth_token)
        app.add_api_route("/events/webhook", self.start_task, methods=["POST"])
        app.add_api_route(
            "/runs/{system}/{task_id}", self.get_run_status, methods=["GET"]
        )
        app.add_api_route("/logs/all", self.get_all_logs, methods=["GET"])
        app.add_api_route(
            "/logs/{system}/{task_id}", self.get_logs, methods=["GET"]
        )
        app.add_api_route("/healthcheck", healthcheck, methods=["GET"])
        return app

    async def start_task(self, request: Request):
        payload = await read_json_object(request)
        task_id, trace_id = request_task_identifiers(payload)
        system = sanitize_system_id(
            request_system(payload, self.config.webhook.default_system)
        )
        payload["system"] = system
        sid = swarm_id(system, task_id)
        with self._start_lock:
            existing = swarm_storage.load_swarm_meta(self.threads_dir, sid)
            rejection = _reject_existing(existing, system, task_id)
            if rejection is not None:
                return rejection
            overloaded = self._overloaded(system, task_id)
            if overloaded is not None:
                return overloaded
            try:
                sid, _pid = swarm_runner.start_swarm_process(
                    self.swarm,
                    system=system,
                    task_id=task_id,
                    payload=payload,
                    trace_id=trace_id,
                )
            except Exception as e:
                raise http_error(500, f"swarm_start_failed: {e!r}") from None
            if self.monitor is not None:
                self.monitor.register_active(sid)
        return {
            "status": "started",
            "payload": payload,
            "system": system,
            "task_id": task_id,
        }

    def _overloaded(self, system: str, task_id: str) -> JSONResponse | None:
        limit = int(self.swarm.get("max_concurrent", 10))
        if self.monitor is None or self.monitor.get_active_count() < limit:
            return None
        return _error_response(
            503,
            "too many concurrent swarm processes, retry later",
            system,
            task_id,
        )

    async def get_run_status(self, system: str, task_id: str):
        sys_norm, task_norm = self._key(system, task_id)
        sid = swarm_id(sys_norm, task_norm)
        pre = swarm_storage.load_swarm_meta(self.threads_dir, sid)
        if not pre:
            raise HTTPException(
                status_code=404, detail={"status": "not_found"}
            )
        meta = (
            swarm_lifecycle.check_and_finalize(
                self.threads_dir,
                self.sandbox_root_dir,
                sid,
                swarm_logs_dir=str(self.swarm.get("swarm_logs_dir", "")),
            )
            or pre
        )
        status = _status_of(meta)
        if status in swarm_lifecycle.FINAL_STATUSES and self.monitor:
            self.monitor.unregister_active(sid)
        return _status_body(meta, sys_norm, task_norm)

    async def get_logs(self, system: str, task_id: str):
        sys_norm, task_norm = self._key(system, task_id)
        sid = swarm_id(sys_norm, task_norm)
        meta = swarm_storage.load_swarm_meta(self.threads_dir, sid)
        # Канал E: messages.jsonl лежит внутри каталога песочницы.
        messages = swarm_storage.load_swarm_messages(
            self.threads_dir, sid, self.sandbox_root_dir
        )
        stdout, stderr = swarm_storage.load_swarm_stdout_stderr(
            self.threads_dir, sid
        )
        if not (meta or messages or stdout or stderr):
            raise HTTPException(
                status_code=404, detail={"status": "not_found"}
            )
        return {
            "status": "ok",
            "system": sys_norm,
            "task_id": task_norm,
            "meta": meta,
            "messages": messages,
            "stdout": stdout,
            "stderr": stderr,
        }

    async def get_all_logs(self):
        out = []
        if os.path.isdir(self.threads_dir):
            for name in sorted(os.listdir(self.threads_dir)):
                meta = swarm_storage.load_swarm_meta(self.threads_dir, name)
                if not meta or not name.startswith("swarm_"):
                    continue
                system, task_id = meta.get("system"), meta.get("task_id")
                out.append(
                    {
                        "system": system,
                        "task_id": task_id,
                        "updated_at": _swarm_meta(meta).get("finished_at")
                        or _swarm_meta(meta).get("started_at"),
                        "logs_url": f"/logs/{system}/{task_id}",
                    }
                )
        out.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
        return {"status": "ok", "logs": out}

    @staticmethod
    def _key(system: str, task_id: str) -> tuple[str, str]:
        sys_norm, task_norm = path_key(system, task_id)
        return sanitize_system_id(sys_norm), task_norm


def _swarm_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    sm = meta.get("swarm_meta") or {}
    return sm if isinstance(sm, dict) else {}


def _status_of(meta: Dict[str, Any]) -> str:
    return str(_swarm_meta(meta).get("status", "")).strip().lower()


def _status_body(
    meta: Dict[str, Any], system: str, task_id: str
) -> Dict[str, Any]:
    """Статус песочницы в общей форме статуса прогона; поля песочницы — в
    ``swarm``."""
    sm = _swarm_meta(meta)
    status = _COMMON_STATUS.get(_status_of(meta), "running")
    body: Dict[str, Any] = {
        "status": status,
        "system": system,
        "task_id": task_id,
        "thread_id": task_id,
        "started_at": sm.get("started_at"),
        "swarm": sm,
    }
    if status == "running":
        return body
    body["finished_at"] = sm.get("finished_at")
    if status == "failed":
        body["error"] = sm.get("error_message") or sm.get("error_type") or ""
    return body


def _error_response(
    status: int, payload: str, system: str, task_id: str, **extra: Any
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "detail": {
                "status": "error",
                "payload": payload,
                "system": system,
                "task_id": task_id,
                **extra,
            }
        },
    )


def _reject_existing(
    existing: Dict[str, Any] | None, system: str, task_id: str
) -> JSONResponse | None:
    """Задача уже идёт — 409 «занято»; уже завершена — 409: песочница
    задачи одноразовая, новой задаче нужен новый ``task_id``."""
    if not existing:
        return None
    status = _status_of(existing)
    if status in _ACTIVE:
        return _error_response(
            409, "task is running", system, task_id, current_status=status
        )
    if status in swarm_lifecycle.FINAL_STATUSES:
        return _error_response(
            409,
            "task already finished — use a new task_id",
            system,
            task_id,
            current_status=status,
        )
    # Неизвестный или повреждённый статус — как новая задача.
    return None
