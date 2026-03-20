import json
import os
from typing import Any, Dict, Optional
import logging

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from run_context import create_run_context  # type: ignore  # noqa: E402
from agent_executor import AgentExecutor  # type: ignore  # noqa: E402
from utils.generate_message_id import (  # type: ignore  # noqa: E402
    generate_msg_id_sha256 as generate_message_id_func,
)
from config import build_auth_connector  # type: ignore  # noqa: E402
from thread_store import (  # type: ignore  # noqa: E402
    load_thread_messages,
    load_thread_meta,
    resolve_threads_dir,
    validate_thread_id,
)
from .rest_api.agent_execution_registry import agent_run_registry  # type: ignore  # noqa: E402
from .rest_api.server_config import (  # type: ignore  # noqa: E402
    PROJECT_ROOT,
    build_agent_config_for_server,
    load_server_config,
)
from .rest_api.utils import (  # type: ignore  # noqa: E402
    ensure_task_id,
    normalize_system,
    patch_asyncio_run_for_fastapi,
)

logger = logging.getLogger(__name__)

# Swarm-режим пока может быть не готов (например, отсутствуют модули/зависимости).
# Чтобы обычный REST-вебхук стартовал всегда, делаем swarm-импорты ленивыми:
# если swarm не импортируется — считаем, что он выключен.
_SWARM_MODULES_AVAILABLE = True
try:
    from .swarm.config import build_swarm_config  # type: ignore  # noqa: E402
    from .swarm.monitor import SwarmMonitor  # type: ignore  # noqa: E402
    from .swarm import runner as swarm_runner  # type: ignore  # noqa: E402
    from .swarm import storage as swarm_storage  # type: ignore  # noqa: E402
except (ModuleNotFoundError, ImportError) as e:
    _SWARM_MODULES_AVAILABLE = False
    logger.warning("swarm disabled due to import error: %r", e)
    build_swarm_config = None  # type: ignore[assignment]
    SwarmMonitor = None  # type: ignore[assignment]
    swarm_runner = None  # type: ignore[assignment]
    swarm_storage = None  # type: ignore[assignment]


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# Настраиваем безопасный asyncio.run для работы внутри FastAPI/uvicorn.
patch_asyncio_run_for_fastapi()


SERVER_CONFIG: Dict[str, Any] = load_server_config()
BASE_AGENT_CONFIG: Dict[str, Any] = build_agent_config_for_server(SERVER_CONFIG)
if _SWARM_MODULES_AVAILABLE and build_swarm_config is not None:
    SWARM_CONFIG: Dict[str, Any] = build_swarm_config(SERVER_CONFIG)
else:
    SWARM_CONFIG = {"enabled": False}


_swarm_monitor: Optional[Any] = None


app = FastAPI()


@app.on_event("startup")
async def _startup_swarm_monitor() -> None:
    """
    Инициализировать фоновый монитор сворм-процессов, если режим включён.
    """
    global _swarm_monitor  # noqa: PLW0603

    if not SWARM_CONFIG.get("enabled"):
        return

    threads_dir = str(SWARM_CONFIG["threads_dir"])
    monitor = SwarmMonitor(threads_dir)
    monitor.start_in_background()
    _swarm_monitor = monitor


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """
    Примитивная токен-авторизация для REST-слоя.

    В server_config.yml можно задать:

    server:
      auth_token: "секретный_токен"

    Если auth_token не задан или пустой, авторизация отключена.
    Если задан, то каждый запрос должен содержать заголовок:

      Authorization: Bearer <auth_token>

    Иначе вернётся 403.
    """
    server_cfg = SERVER_CONFIG.get("server") or {}
    auth_token = server_cfg.get("auth_token")
    if not auth_token:
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or token != str(auth_token):
        return JSONResponse(
            status_code=403,
            content={"status": "forbidden", "detail": "invalid or missing token"},
        )

    return await call_next(request)


@app.get("/runs/{system}/{task_id}")
async def get_run_status(system: str, task_id: str):
    sys_norm = str(system or "").lower().strip()
    task_norm = str(task_id or "").strip()
    if not sys_norm or not task_norm:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": "system and task_id are required"},
        )

    run = agent_run_registry.get_status(system=sys_norm, task_id=task_norm)
    if run is None:
        raise HTTPException(status_code=404, detail={"status": "not_found"})

    if not run.done:
        return {
            "status": "running",
            "system": sys_norm,
            "task_id": task_norm,
            "thread_id": run.thread_id,
            "started_at": run.started_at,
            "last_activity_at": run.last_activity_at,
        }

    if run.error is not None:
        return {
            "status": "failed",
            "system": sys_norm,
            "task_id": task_norm,
            "thread_id": run.thread_id,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "error": repr(run.error),
        }

    return {
        "status": "completed",
        "system": sys_norm,
        "task_id": task_norm,
        "thread_id": run.thread_id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "result": run.result,
    }


@app.post("/events/webhook")
async def events_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": "invalid JSON body"},
        )

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": "JSON body must be an object"},
        )

    task_id = ensure_task_id(payload)

    # Определяем/нормализуем system: берём из payload["system"] или defaults.system.
    source_value = payload.get("system")
    defaults = SERVER_CONFIG.get("defaults") or {}
    default_system = str(defaults.get("system", "generic"))
    system = normalize_system(source_value, default_system=default_system)
    payload["system"] = system

    message_str = json.dumps(payload, ensure_ascii=False)

    def _make_user_message(content: str) -> Dict[str, Any]:
        return {
            "role": "user",
            "content": content,
            "id": generate_message_id_func("m"),
        }

    if agent_run_registry.add_user_message(
        system=system,
        task_id=task_id,
        make_message=_make_user_message,
        content=message_str,
    ):
        return {
            "status": "queued",
            "payload": payload,
            "system": system,
            "task_id": task_id,
        }

    config_dict = dict(BASE_AGENT_CONFIG)
    auth_connector = build_auth_connector(config_dict)
    ctx = create_run_context(config_dict, auth_connector=auth_connector)

    agent_run_registry.start_if_absent(
        system=system,
        task_id=task_id,
        executor_factory=AgentExecutor,
        ctx=ctx,
        initial_user_message=message_str,
        thread_id=task_id,
        verbose=None,
    )

    return {
        "status": "started",
        "payload": payload,
        "system": system,
        "task_id": task_id,
    }


@app.get("/logs/{system}/{task_id}")
async def get_thread_logs(system: str, task_id: str):
    """
    Отдать логи работы агента по связке (system, task_id).

    Возвращает JSON:
    {
      "status": "ok",
      "system": "...",
      "task_id": "...",
      "meta": { ... },        # содержимое meta_<thread_id>.json (или {} если нет)
      "messages": [ ... ]     # список сообщений из data_<thread_id>.jsonl (payload'ы)
    }

    В качестве thread_id используется task_id (как и в запуске через AgentRunRegistry).
    """
    sys_norm = str(system or "").lower().strip()
    try:
        task_norm = validate_thread_id(str(task_id or ""))
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": str(e)},
        )

    if not sys_norm:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": "system is required"},
        )

    try:
        threads_dir = resolve_threads_dir(BASE_AGENT_CONFIG)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "payload": f"threads resolve error: {e!r}"},
        )

    meta = load_thread_meta(threads_dir, sys_norm, task_norm)
    messages, _, _ = load_thread_messages(threads_dir, sys_norm, task_norm)

    if not meta and not messages:
        raise HTTPException(status_code=404, detail={"status": "not_found"})

    return {
        "status": "ok",
        "system": sys_norm,
        "task_id": task_norm,
        "meta": meta,
        "messages": messages,
    }


@app.get("/logs/all")
async def get_all_thread_logs():
    """
    Выдать список всех доступных тредов (system + task_id).

    Служебный эндпоинт для режима вебхука: system можно заранее не знать,
    а также удобно построить UI "журнала".
    """
    try:
        threads_dir = resolve_threads_dir(BASE_AGENT_CONFIG)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "payload": f"threads resolve error: {e!r}"},
        )

    out: list[dict[str, Any]] = []
    try:
        names = [n for n in os.listdir(threads_dir) if n.startswith("meta_") and n.endswith(".json")]
    except FileNotFoundError:
        return {"status": "ok", "logs": []}

    for name in names:
        meta_path = os.path.join(threads_dir, name)
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue

        if not isinstance(meta, dict):
            continue

        system = meta.get("system")
        if not isinstance(system, str) or not system.strip():
            system = "unknown"

        task_id = meta.get("thread_id")
        if not isinstance(task_id, str) or not task_id.strip():
            # legacy fallback: meta_<thread_id>.json
            task_id = name.removeprefix("meta_").removesuffix(".json")

        updated_at = meta.get("updated_at") or meta.get("created_at")
        messages_count = meta.get("messages_count")

        out.append(
            {
                "system": system,
                "task_id": task_id,
                "updated_at": updated_at,
                "messages_count": messages_count,
                "logs_url": f"/logs/{system}/{task_id}",
            }
        )

    out.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return {"status": "ok", "logs": out}


@app.post("/swarm/run")
async def swarm_run(request: Request):
    """
    Запуск отдельного сворм-процесса (CLI в песочнице).
    """
    if not SWARM_CONFIG.get("enabled"):
        raise HTTPException(
            status_code=503,
            detail={"status": "error", "reason": "swarm_disabled"},
        )

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": "invalid JSON body"},
        )

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": "JSON body must be an object"},
        )

    # Нормализуем system и task_id по тем же правилам, что и в вебхуке.
    task_id = ensure_task_id(payload)
    source_value = payload.get("system")
    defaults = SERVER_CONFIG.get("defaults") or {}
    default_system = str(defaults.get("system", "generic"))
    system = normalize_system(source_value, default_system=default_system)
    payload["system"] = system

    # Ограничение по количеству одновременных свормов.
    max_concurrent = int(SWARM_CONFIG.get("max_concurrent", 10))
    monitor = _swarm_monitor
    if monitor is not None:
        active_count = monitor.get_active_count()
        if active_count >= max_concurrent:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "rejected",
                    "reason": "too_many_concurrent_swarm_processes",
                    "system": system,
                    "task_id": task_id,
                },
            )

    try:
        swarm_id, _pid = swarm_runner.start_swarm_process(
            SWARM_CONFIG, system=system, task_id=task_id, payload=payload
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "reason": f"swarm_start_failed: {e!r}"},
        )

    if monitor is not None:
        monitor.register_active(swarm_id)

    return {
        "status": "started",
        "system": system,
        "task_id": task_id,
    }


@app.get("/swarm/status/{system}/{task_id}")
async def get_swarm_status(system: str, task_id: str):
    """
    Получить статус сворм-задачи.
    """
    if not SWARM_CONFIG.get("enabled"):
        raise HTTPException(
            status_code=503,
            detail={"status": "error", "reason": "swarm_disabled"},
        )

    sys_norm = str(system or "").lower().strip()
    task_norm = str(task_id or "").strip()
    if not sys_norm or not task_norm:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": "system and task_id are required"},
        )

    swarm_id = f"{sys_norm}_{task_norm}"
    threads_dir = str(SWARM_CONFIG["threads_dir"])
    meta = swarm_storage.load_swarm_meta(threads_dir, swarm_id)
    if not meta:
        raise HTTPException(status_code=404, detail={"status": "not_found"})

    swarm_meta = meta.get("swarm_meta") or {}
    if not isinstance(swarm_meta, dict):
        swarm_meta = {}
    status = str(swarm_meta.get("status", "")).strip().lower() or "unknown"

    return {
        "status": status,
        "system": sys_norm,
        "task_id": task_norm,
        "meta": meta,
    }


@app.get("/swarm/logs/{system}/{task_id}")
async def get_swarm_logs(system: str, task_id: str):
    """
    Получить логи сворм-задачи (метадата + сообщения).
    """
    if not SWARM_CONFIG.get("enabled"):
        raise HTTPException(
            status_code=503,
            detail={"status": "error", "reason": "swarm_disabled"},
        )

    sys_norm = str(system or "").lower().strip()
    task_norm = str(task_id or "").strip()
    if not sys_norm or not task_norm:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": "system and task_id are required"},
        )

    swarm_id = f"{sys_norm}_{task_norm}"
    threads_dir = str(SWARM_CONFIG["threads_dir"])
    meta = swarm_storage.load_swarm_meta(threads_dir, swarm_id)
    messages = swarm_storage.load_swarm_messages(threads_dir, swarm_id)
    stdout_lines, stderr_lines = swarm_storage.load_swarm_stdout_stderr(
        threads_dir, swarm_id
    )

    if not meta and not messages and not stdout_lines and not stderr_lines:
        raise HTTPException(status_code=404, detail={"status": "not_found"})

    return {
        "status": "ok",
        "system": sys_norm,
        "task_id": task_norm,
        "meta": meta,
        "messages": messages,
        "stdout": stdout_lines,
        "stderr": stderr_lines,
    }


@app.get("/healthcheck")
async def healthcheck():
    return {"status": "ok"}


if __name__ == "__main__":
    server_cfg = SERVER_CONFIG.get("server", {}) or {}
    host = str(server_cfg.get("host", "0.0.0.0"))
    port_raw = server_cfg.get("port", 8000)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 8000

    uvicorn.run("src.server:app", host=host, port=port)

