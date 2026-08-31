import json
import os
import threading
from typing import Any, Dict, Optional
import logging

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from gc_core.run_context import create_run_context
from gc_core.agent_executor import AgentExecutor
from gc_core.utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func
from gc_core.config import build_auth_connector, cli_forces_legacy_model
from gc_core.thread_store import load_thread_messages, load_thread_meta, resolve_threads_dir, validate_thread_id
from gc_server.rest_api.agent_execution_registry import agent_run_registry
from gc_server.rest_api.server_config import build_agent_config_for_server, load_server_config
from gc_server.rest_api.utils import ensure_task_id, normalize_system, patch_asyncio_run_for_fastapi
from gc_server.server_cleanup_threads import register_cleanup_threads_on_startup, register_swarm_cleanup_on_startup
from gc_core.trace_id import normalize_trace_id

logger = logging.getLogger(__name__)

# Swarm-режим пока может быть не готов (например, отсутствуют модули/зависимости).
# Чтобы обычный REST-вебхук стартовал всегда, делаем swarm-импорты ленивыми:
# если swarm не импортируется — считаем, что он выключен.
_SWARM_MODULES_AVAILABLE = True
try:
    from gc_server.swarm.config import build_swarm_config
    from gc_server.swarm.monitor import SwarmMonitor
    from gc_server.swarm import lifecycle as swarm_lifecycle
    from gc_server.swarm import runner as swarm_runner
    from gc_server.swarm import storage as swarm_storage
except (ModuleNotFoundError, ImportError) as e:
    _SWARM_MODULES_AVAILABLE = False
    logger.warning("swarm disabled due to import error: %r", e)
    build_swarm_config = None  # type: ignore[assignment]
    SwarmMonitor = None  # type: ignore[assignment]
    swarm_lifecycle = None  # type: ignore[assignment]
    swarm_runner = None  # type: ignore[assignment]
    swarm_storage = None  # type: ignore[assignment]


# Настраиваем безопасный asyncio.run для работы внутри FastAPI/uvicorn.
patch_asyncio_run_for_fastapi()


SERVER_CONFIG: Dict[str, Any] = load_server_config()
BASE_AGENT_CONFIG: Dict[str, Any] = build_agent_config_for_server(SERVER_CONFIG)
if _SWARM_MODULES_AVAILABLE and build_swarm_config is not None:
    SWARM_CONFIG: Dict[str, Any] = build_swarm_config(SERVER_CONFIG)
else:
    SWARM_CONFIG = {"enabled": False}


_swarm_monitor: Optional[Any] = None

# Глобальный lock для idempotent POST /swarm/run (шаг 1.8, AR-3).
# Защищает: read meta → решение reject vs start → save meta + Popen.
# Под нагрузкой можно перейти на Dict[swarm_id, Lock] — на MVP это overkill.
_SWARM_RUN_LOCK = threading.Lock()


app = FastAPI()


register_cleanup_threads_on_startup(
    app,
    server_config=SERVER_CONFIG,
    base_agent_config=BASE_AGENT_CONFIG,
    logger=logger,
)

# Шаг 3.2 — TTL-janitor для swarm_threads/ + .swarm_sandboxes/.
register_swarm_cleanup_on_startup(
    app,
    swarm_config=SWARM_CONFIG,
    logger=logger,
)


@app.on_event("startup")
async def _startup_swarm_monitor() -> None:
    """
    Инициализировать фоновый монитор сворм-процессов, если режим включён.
    """
    global _swarm_monitor  # noqa: PLW0603

    if not SWARM_CONFIG.get("enabled"):
        return

    threads_dir = str(SWARM_CONFIG["threads_dir"])
    sandbox_root_dir = str(SWARM_CONFIG["sandbox_root_dir"])
    monitor = SwarmMonitor(
        threads_dir,
        sandbox_root_dir,
        swarm_logs_dir=str(SWARM_CONFIG.get("swarm_logs_dir", "")),
    )
    monitor.start_in_background()
    _swarm_monitor = monitor


@app.on_event("shutdown")
async def _shutdown_swarm_processes() -> None:
    """Graceful shutdown: убить все живые sworm-процессы (шаг 3.3).

    Без этого хендлера живые sworm'ы становятся сиротами после остановки
    сервера (Ctrl+C, systemd stop, k8s pod terminate).

    Алгоритм:
    1. Снимаем снапшот active_ids из monitor'а.
    2. Каждому посылаем SIGTERM через os.killpg (PID == PGID благодаря
       start_new_session=True в runner.py, шаг 1.6).
    3. asyncio.sleep grace-period 3 секунды.
    4. Кто остался жив — SIGKILL.
    5. Обновляем meta: status=unknown_failed, error_type=server_shutdown.
    """
    import asyncio
    import os
    import signal

    monitor = _swarm_monitor
    if monitor is None or not SWARM_CONFIG.get("enabled"):
        return

    active_ids = list(monitor.get_active_ids_copy())
    if not active_ids:
        return

    threads_dir = str(SWARM_CONFIG["threads_dir"])
    logger.info("shutdown: terminating %d active sworm(s)", len(active_ids))

    # 1. SIGTERM для всех.
    pids_to_kill = []
    for swarm_id in active_ids:
        meta = swarm_storage.load_swarm_meta(threads_dir, swarm_id)
        if not meta:
            continue
        sm = meta.get("swarm_meta") or {}
        pid = sm.get("pid")
        if isinstance(pid, int) and swarm_lifecycle._is_pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGTERM)
                pids_to_kill.append((swarm_id, pid))
            except (ProcessLookupError, PermissionError, OSError):
                pass

    # 2. Grace period — CLI пытается дописать summary.
    await asyncio.sleep(3.0)

    # 3. Добиваем оставшихся + обновляем meta.
    for swarm_id, pid in pids_to_kill:
        if swarm_lifecycle._is_pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

        # Обновляем meta — даже если процесс уже умер, обновляем статус
        # на server_shutdown для аудита.
        try:
            existing = swarm_storage.load_swarm_meta(threads_dir, swarm_id)
            if existing:
                sm = existing.get("swarm_meta") or {}
                current_status = str(sm.get("status", "")).strip().lower()
                if current_status not in swarm_lifecycle.FINAL_STATUSES:
                    swarm_storage.save_swarm_meta(
                        threads_dir, swarm_id,
                        base_meta={
                            "system": existing.get("system"),
                            "task_id": existing.get("task_id"),
                        },
                        swarm_meta_patch={
                            "status": "unknown_failed",
                            "finished_at": swarm_storage._utc_now_iso(),  # noqa: SLF001
                            "error_type": "server_shutdown",
                            "error_message": "Killed during server shutdown",
                            "sandbox_status": "completed",
                        },
                    )
        except Exception:  # noqa: BLE001
            logger.exception("shutdown: failed to update meta for %s", swarm_id)

        monitor.unregister_active(swarm_id)

    # Останавливаем фоновой loop.
    monitor.stop()


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

    # trace_id — поле контекста задачи, а не часть запроса к модели: снимаем его
    # ДО json.dumps(payload) ниже, иначе значение уедет в промпт модели.
    try:
        trace_id = normalize_trace_id(payload.pop("trace_id", None))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": f"invalid trace_id: {exc}"},
        )

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
        if trace_id:
            # Единственное место в дизайне, где trace может быть легитимно
            # потерян: запрос подмешан в живой ран, чьи ctx, логгер, имя
            # лог-файла и конфиг MCP-серверов уже зафиксированы. Поэтому у
            # потери есть именованное событие, а не молчание.
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
        return {
            "status": "queued",
            "payload": payload,
            "system": system,
            "task_id": task_id,
        }

    config_dict = dict(BASE_AGENT_CONFIG)
    if trace_id:
        # Shallow copy достаточно: значение — скаляр. Мутировать
        # BASE_AGENT_CONFIG нельзя, он процесс-глобальный.
        config_dict["trace_id"] = trace_id
    auth_connector = build_auth_connector(config_dict)
    # Легаси-форс (спека §2): agent_overrides.model/completions_url поверх непустого
    # реестра моделей — тот же контракт, что CLI --model (agent_overrides перекрывают
    # config.yml так же, как CLI-аргументы).
    force_legacy = cli_forces_legacy_model(config_dict)
    if force_legacy:
        logger.warning("agent_overrides: уровни отключены на этот запуск (легаси-форс)")
    ctx = create_run_context(config_dict, auth_connector=auth_connector, force_legacy=force_legacy)

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
    # trace_id — поле контекста задачи, а не часть запроса к модели. Снимаем его
    # ДО того, как payload уедет в песочницу целиком: он доставляется ребёнку
    # аргументом `-q`, то есть оставленный в теле трейс попал бы в промпт модели
    # как текст задачи. Симметрично вебхуку.
    try:
        trace_id = normalize_trace_id(payload.pop("trace_id", None))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": f"invalid trace_id: {exc}"},
        )
    source_value = payload.get("system")
    defaults = SERVER_CONFIG.get("defaults") or {}
    default_system = str(defaults.get("system", "generic"))
    system = normalize_system(source_value, default_system=default_system)
    payload["system"] = system
    swarm_id = f"swarm_{system}_{task_id}"

    # AR-3: reuse_strategy MVP — поддерживаем только "reject".
    # "continue" и "fresh" — отдадим 501, чтобы клиент не зависел от непостроенной семантики.
    reuse_strategy = str(payload.get("reuse_strategy") or "reject").strip().lower()
    if reuse_strategy not in {"reject", ""}:
        return JSONResponse(
            status_code=501,
            content={
                "status": "not_implemented",
                "reason": f"reuse_strategy_{reuse_strategy}_not_supported",
                "supported_strategies": ["reject"],
            },
        )

    threads_dir = str(SWARM_CONFIG["threads_dir"])
    max_concurrent = int(SWARM_CONFIG.get("max_concurrent", 10))
    monitor = _swarm_monitor

    # Idempotency + concurrent-start защита под глобальным lock'ом (шаг 1.8).
    # Под lock'ом делаем: чтение meta → решение reject vs start → запуск Popen + register.
    # Это закрывает 3 проблемы:
    #   1) обход max_concurrent через повторные POST с одним task_id;
    #   2) race condition между параллельными POST с одним swarm_id;
    #   3) перезапись meta живого прогона.
    with _SWARM_RUN_LOCK:
        existing = swarm_storage.load_swarm_meta(threads_dir, swarm_id)
        if existing:
            sm = existing.get("swarm_meta") or {}
            if not isinstance(sm, dict):
                sm = {}
            current_status = str(sm.get("status", "")).strip().lower()
            if current_status in {"queued", "running"}:
                return JSONResponse(
                    status_code=409,
                    content={
                        "status": "rejected",
                        "reason": "swarm_id_already_active",
                        "system": system,
                        "task_id": task_id,
                        "current_status": current_status,
                    },
                )
            # Финальный статус → reject (AR-3).
            if current_status in swarm_lifecycle.FINAL_STATUSES:
                return JSONResponse(
                    status_code=409,
                    content={
                        "status": "rejected",
                        "reason": "swarm_id_already_finalized",
                        "system": system,
                        "task_id": task_id,
                        "current_status": current_status,
                    },
                )
            # Неизвестный/повреждённый статус — пропускаем как fresh start.

        # Лимит на количество одновременно работающих свормов.
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
                SWARM_CONFIG,
                system=system,
                task_id=task_id,
                payload=payload,
                trace_id=trace_id,
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

    swarm_id = f"swarm_{sys_norm}_{task_norm}"
    threads_dir = str(SWARM_CONFIG["threads_dir"])
    sandbox_root_dir = str(SWARM_CONFIG["sandbox_root_dir"])

    # Сначала проверяем, существует ли sworm. Если нет — 404 без финализации.
    pre_meta = swarm_storage.load_swarm_meta(threads_dir, swarm_id)
    if not pre_meta:
        raise HTTPException(status_code=404, detail={"status": "not_found"})

    # Lazy-чек: даже если monitor ещё не успел финализировать, status-эндпоинт
    # подтянет completion.json или проверит PID и обновит meta при необходимости.
    # См. swarm_spec.md §5.2 и шаг M2/1.5.
    meta = swarm_lifecycle.check_and_finalize(
        threads_dir, sandbox_root_dir, swarm_id,
        swarm_logs_dir=str(SWARM_CONFIG.get("swarm_logs_dir", "")),
    )
    if not meta:
        # Защитный фоллбек — теоретически невозможно, т.к. pre_meta был.
        meta = pre_meta

    swarm_meta = meta.get("swarm_meta") or {}
    if not isinstance(swarm_meta, dict):
        swarm_meta = {}
    status = str(swarm_meta.get("status", "")).strip().lower() or "unknown"

    # Если после lazy-чека статус стал финальным — снять с monitor'а.
    if status in swarm_lifecycle.FINAL_STATUSES and _swarm_monitor is not None:
        _swarm_monitor.unregister_active(swarm_id)

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

    swarm_id = f"swarm_{sys_norm}_{task_norm}"
    threads_dir = str(SWARM_CONFIG["threads_dir"])
    sandbox_root_dir = str(SWARM_CONFIG.get("sandbox_root_dir", ""))
    meta = swarm_storage.load_swarm_meta(threads_dir, swarm_id)
    # Канал E (шаг 1.9): messages.jsonl лежит внутри sandbox-каталога.
    messages = swarm_storage.load_swarm_messages(
        threads_dir, swarm_id, sandbox_root_dir
    )
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


def serve() -> int:
    """Поднять REST-сервер по config_server.yml. Тело точки входа gc-serve."""
    server_cfg = SERVER_CONFIG.get("server", {}) or {}
    host = str(server_cfg.get("host", "0.0.0.0"))
    port_raw = server_cfg.get("port", 8000)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 8000

    # Было "src.server:app": под установленным колесом (sources = ["src"]) модуль
    # называется server, поэтому строка резолвилась только при запуске из корня
    # репозитория, а из wheel падала ModuleNotFoundError.
    uvicorn.run("gc_server.server:app", host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())

