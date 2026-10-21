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

from fastapi import FastAPI, HTTPException
from gc_core.threads.models import ThreadKey

from gc_server.config import ServerConfigError
from gc_server.surface import http_error, install_bearer
from gc_server.swarm import lifecycle as swarm_lifecycle
from gc_server.swarm import runner as swarm_runner
from gc_server.swarm import storage as swarm_storage
from gc_server.swarm.config import build_swarm_config
from gc_server.swarm.monitor import SwarmMonitor
from gc_server.webhook.janitor import start_swarm_cleanup
from gc_server.webhook.surface import (
    healthcheck,
    path_key,
)

if TYPE_CHECKING:
    from gc_server.app import ServerRuntime

logger = logging.getLogger(__name__)

#: Статус песочницы → общий статус задачи (как у вебхука).
_COMMON_STATUS = {
    "queued": "running",
    "running": "running",
    "cancelled": "cancelled",
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
        thread_store = _sandbox_thread_store(runtime)
        if thread_store is not None:
            self.swarm = {**self.swarm, "thread_store": thread_store}
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
                self._mark_final(
                    sid,
                    "unknown_failed",
                    "server_shutdown",
                    "Killed during server shutdown",
                )
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

    def _mark_final(
        self, sid: str, status: str, error_type: str, error_message: str
    ) -> None:
        """Записать песочнице финальный статус, если его ещё нет."""
        try:
            existing = swarm_storage.load_swarm_meta(self.threads_dir, sid)
            if not existing:
                return
            if _status_of(existing) in swarm_lifecycle.FINAL_STATUSES:
                return
            swarm_storage.save_swarm_meta(
                self.threads_dir,
                sid,
                base_meta={
                    "system": existing.get("system"),
                    "task_id": existing.get("task_id"),
                },
                swarm_meta_patch={
                    "status": status,
                    "finished_at": swarm_storage._utc_now_iso(),
                    "error_type": error_type,
                    "error_message": error_message,
                    "sandbox_status": "completed",
                },
            )
        except Exception:
            logger.exception("swarm: failed to update meta for %s", sid)

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
        app.add_api_route(
            "/v1/tasks/{system}/{task_id}/output",
            self.get_output,
            methods=["GET"],
        )
        app.add_api_route("/healthcheck", healthcheck, methods=["GET"])
        return app

    @property
    def default_system(self) -> str:
        return self.config.webhook.default_system

    @property
    def signing(self) -> Any:
        """Подпись тела запросов — секция webhook (общая с swarm)."""
        return self.runtime.config.webhook

    @property
    def events_poll_seconds(self) -> float:
        return 1.0

    async def asubmit(
        self,
        key: ThreadKey,
        message: Dict[str, Any],
        *,
        op_id: str | None,
        trace_id: str | None,
        callback_url: str | None = None,
    ) -> Dict[str, Any]:
        if callback_url is not None:
            raise http_error(400, "callback_url is not supported by swarm")
        sid = swarm_id(key.system, key.thread)
        await self._reject_foreign_thread(key)
        if self._in_store and self._active(sid):
            # Тред задачи в общей базе: сообщение — во входящий поток, его
            # заберёт ход gc-cli в песочнице (опросом ядра).
            receipt = await self.runtime.threads("swarm").submit(
                key, message, op_id=op_id, source="swarm"
            )
            return {
                "status": "queued",
                "message_seq": receipt.max_seq.get("inbox"),
            }
        with self._start_lock:
            existing = swarm_storage.load_swarm_meta(self.threads_dir, sid)
            _reject_existing(existing, key.system, key.thread)
            self._reject_overload(key.system, key.thread)
            try:
                sid, _pid = swarm_runner.start_swarm_process(
                    self.swarm,
                    system=key.system,
                    task_id=key.thread,
                    payload={},
                    trace_id=trace_id,
                    query=message["content"],
                )
            except Exception as e:
                raise http_error(500, f"swarm_start_failed: {e!r}") from None
            if self.monitor is not None:
                self.monitor.register_active(sid)
        if self._in_store:
            await self.runtime.threads("swarm").update_frontend(
                key, "swarm", {"swarm_id": sid}
            )
        return {"status": "started", "message_seq": None}

    async def astatus(self, key: ThreadKey) -> Dict[str, Any] | None:
        sid = swarm_id(key.system, key.thread)
        pre = swarm_storage.load_swarm_meta(self.threads_dir, sid)
        if not pre:
            return None
        meta = (
            swarm_lifecycle.check_and_finalize(
                self.threads_dir,
                self.sandbox_root_dir,
                sid,
                swarm_logs_dir=str(self.swarm.get("swarm_logs_dir", "")),
            )
            or pre
        )
        if _status_of(meta) in swarm_lifecycle.FINAL_STATUSES and self.monitor:
            self.monitor.unregister_active(sid)
        body = _status_body(meta, key.system, key.thread)
        body["pending_messages"] = await self._pending(key)
        return body

    async def acancel(self, key: ThreadKey) -> Dict[str, Any] | None:
        """Отмена задачи: SIGTERM группе процессов песочницы (затем SIGKILL)
        и статус ``cancelled``."""
        status = await self.astatus(key)
        if status is None:
            return None
        if status["status"] != "running":
            return {"status": status["status"]}
        sid = swarm_id(key.system, key.thread)
        killed = self._terminate_groups([sid])
        self._mark_final(sid, "cancelled", "cancelled", "Cancelled by request")
        if self.monitor is not None:
            self.monitor.unregister_active(sid)
        if killed:
            asyncio.get_running_loop().create_task(self._akill_later(killed))
        return {"status": "cancelling"}

    async def alist(
        self, *, system: str | None, limit: int, cursor: str | None
    ) -> tuple[list[Dict[str, Any]], str | None]:
        tasks = [
            entry
            for entry in self._task_entries()
            if system is None or entry["system"] == system
        ]
        start = int(cursor) if cursor and cursor.isdigit() else 0
        page = tasks[start : start + limit]
        more = start + limit < len(tasks)
        return page, str(start + limit) if more else None

    async def amessages(
        self, key: ThreadKey, *, after: int, limit: int
    ) -> list[Dict[str, Any]] | None:
        sid = swarm_id(key.system, key.thread)
        if not swarm_storage.load_swarm_meta(self.threads_dir, sid):
            return None
        if self._in_store:
            return await self._store_messages(key, after=after, limit=limit)
        # Канал E: messages.jsonl лежит внутри каталога песочницы.
        messages = swarm_storage.load_swarm_messages(
            self.threads_dir, sid, self.sandbox_root_dir
        )
        entries = [
            {"seq": seq, "ts": None, "message": message}
            for seq, message in enumerate(messages, start=1)
        ]
        return [e for e in entries if e["seq"] > after][:limit]

    def _reject_overload(self, system: str, task_id: str) -> None:
        limit = int(self.swarm.get("max_concurrent", 10))
        if self.monitor is None or self.monitor.get_active_count() < limit:
            return
        raise http_error(
            503,
            "too many concurrent swarm processes, retry later",
            system=system,
            task_id=task_id,
        )

    async def get_output(self, system: str, task_id: str):
        """stdout и stderr процесса песочницы."""
        sys_norm, task_norm = path_key(system, task_id)
        sid = swarm_id(sys_norm, task_norm)
        if not swarm_storage.load_swarm_meta(self.threads_dir, sid):
            raise HTTPException(
                status_code=404, detail={"status": "not_found"}
            )
        stdout, stderr = swarm_storage.load_swarm_stdout_stderr(
            self.threads_dir, sid
        )
        return {"stdout": stdout, "stderr": stderr}

    async def _store_messages(
        self, key: ThreadKey, *, after: int, limit: int
    ) -> list[Dict[str, Any]]:
        # Только тред задачи swarm: тот же (system, task_id) у вебхука —
        # чужая задача.
        threads = self.runtime.threads("swarm")
        record = await threads.get(key)
        if record is None or "swarm" not in record.frontend:
            return []
        entries = await threads.store.read_stream(
            key, "messages", after_seq=after, limit=limit
        )
        return [{"seq": e.seq, "ts": e.ts, "message": e.data} for e in entries]

    async def _reject_foreign_thread(self, key: ThreadKey) -> None:
        """Тред с тем же (system, task_id) без пометки swarm — задача
        вебхука: песочница читала бы и дописывала чужую историю."""
        if not self._in_store:
            return
        record = await self.runtime.threads("swarm").get(key)
        if record is not None and "swarm" not in record.frontend:
            raise http_error(
                409,
                "task belongs to the webhook surface",
                system=key.system,
                task_id=key.thread,
            )

    async def _pending(self, key: ThreadKey) -> int:
        if not self._in_store:
            return 0
        record = await self.runtime.threads("swarm").get(key)
        if record is None:
            return 0
        return max(0, record.max_seq.get("inbox", 0) - record.state.inbox_seq)

    def _task_entries(self) -> list[Dict[str, Any]]:
        out = []
        if os.path.isdir(self.threads_dir):
            for name in sorted(os.listdir(self.threads_dir)):
                meta = swarm_storage.load_swarm_meta(self.threads_dir, name)
                if not meta or not name.startswith("swarm_"):
                    continue
                sm = _swarm_meta(meta)
                out.append(
                    {
                        "system": meta.get("system"),
                        "task_id": meta.get("task_id"),
                        "status": _COMMON_STATUS.get(
                            _status_of(meta), "running"
                        ),
                        "updated_at": sm.get("finished_at")
                        or sm.get("started_at"),
                    }
                )
        out.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
        return out

    async def _akill_later(self, killed: list[tuple[str, int]]) -> None:
        await asyncio.sleep(_KILL_GRACE_S)
        for _sid, pid in killed:
            if swarm_lifecycle._is_pid_alive(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass

    @property
    def _in_store(self) -> bool:
        """Тред задачи — в общей базе (песочница ведёт его там)."""
        return "thread_store" in self.swarm

    def _active(self, sid: str) -> bool:
        meta = swarm_storage.load_swarm_meta(self.threads_dir, sid)
        return bool(meta) and _status_of(meta) in _ACTIVE


def _sandbox_thread_store(runtime: ServerRuntime) -> Dict[str, Any] | None:
    """Подключение песочницы к общей базе — только явная секция
    ``swarm.storage`` (свои, урезанные права): агент в песочнице может
    прочитать своё окружение, учётные данные сервера туда не уходят. Без
    секции — тред в файлах песочницы, как раньше."""
    from gc_core.settings import settings_of

    part = (runtime.config.swarm.model_extra or {}).get("storage")
    if not part:
        return None
    if not isinstance(part, dict) or (
        part.get("type") not in swarm_runner.SANDBOX_STORES
    ):
        raise ServerConfigError(
            "swarm.storage: нужна сетевая база песочницы — type: "
            + ", ".join(sorted(swarm_runner.SANDBOX_STORES))
        )
    kind, params = settings_of(runtime.base).storage.part("threads")
    if kind not in swarm_runner.SANDBOX_STORES:
        logger.warning(
            "swarm.storage задан, а треды сервера — %s: /logs не увидит "
            "тред песочницы; песочница остаётся на своих файлах",
            kind,
        )
        return None
    _require_server_database((kind, params), part)
    return dict(part)


def _require_server_database(
    server: tuple[str, Dict[str, Any]], part: Dict[str, Any]
) -> None:
    """Песочница пишет тред туда, откуда его читают /logs и очередь
    сообщений сервера: тот же тип и то же место данных, учётная запись —
    своя. Иначе — ошибка конфигурации при старте."""
    from gc_server.swarm import store_location

    params = {k: v for k, v in part.items() if k != "type"}
    sandbox = store_location.location(str(part["type"]), params)
    threads = store_location.location(*server)
    if sandbox != threads:
        raise ServerConfigError(
            "swarm.storage указывает не на базу тредов сервера "
            f"(storage.threads: {store_location.describe(threads)}; "
            f"swarm.storage: {store_location.describe(sandbox)}): /logs и "
            "очередь сообщений не увидели бы тред песочницы. Нужны тот же "
            "type и та же база, учётная запись — своя"
        )


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


def _reject_existing(
    existing: Dict[str, Any] | None, system: str, task_id: str
) -> None:
    """Задача уже идёт — 409 «занято»; уже завершена — 409: песочница
    задачи одноразовая, новой задаче нужен новый ``task_id``. Неизвестный
    или повреждённый статус — как новая задача."""
    status = _status_of(existing) if existing else ""
    if status in _ACTIVE:
        raise http_error(
            409,
            "task is running",
            system=system,
            task_id=task_id,
            current_status=status,
        )
    if status in swarm_lifecycle.FINAL_STATUSES:
        raise http_error(
            409,
            "task already finished — use a new task_id",
            system=system,
            task_id=task_id,
            current_status=status,
        )
