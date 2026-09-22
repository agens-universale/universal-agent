import asyncio
import json
import logging
import os
import threading
from typing import Any, Dict, Optional

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from gc_core.agent_executor import AgentExecutor
from gc_core.config import build_auth_connector, cli_forces_legacy_model
from gc_core.run_context import acreate_run_context
from gc_core.thread_store import (
    load_thread_messages,
    load_thread_meta,
    resolve_threads_dir,
    sanitize_system_id,
    validate_thread_id,
)
from gc_core.trace_id import normalize_trace_id
from gc_core.utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func

from gc_server.rest_api.agent_execution_registry import agent_run_registry
from gc_server.rest_api.server_config import build_agent_config_for_server, load_server_config
from gc_server.rest_api.utils import ensure_task_id, normalize_system
from gc_server.server_cleanup_threads import register_cleanup_threads_on_startup, register_swarm_cleanup_on_startup

# Константы вместо повторяющихся литералов (Sonar S1192).
_ERR_SYSTEM_AND_TASK_ID = "system and task_id are required"


logger = logging.getLogger(__name__)

# Swarm-режим пока может быть не готов (например, отсутствуют модули/зависимости).
# Чтобы обычный REST-вебхук стартовал всегда, делаем swarm-импорты ленивыми:
# если swarm не импортируется — считаем, что он выключен.
_SWARM_MODULES_AVAILABLE = True
try:
    from gc_server.swarm import lifecycle as swarm_lifecycle
    from gc_server.swarm import runner as swarm_runner
    from gc_server.swarm import storage as swarm_storage
    from gc_server.swarm.config import build_swarm_config
    from gc_server.swarm.monitor import SwarmMonitor
except ImportError as e:
    _SWARM_MODULES_AVAILABLE = False
    logger.warning("swarm disabled due to import error: %r", e)
    build_swarm_config = None  # type: ignore[assignment]
    SwarmMonitor = None  # type: ignore[assignment]
    swarm_lifecycle = None  # type: ignore[assignment]
    swarm_runner = None  # type: ignore[assignment]
    swarm_storage = None  # type: ignore[assignment]


try:
    # Обе функции читают конфиг на импорте модуля: сломанный config_server.yml
    # (load_server_config сам не защищён — голый open/yaml.safe_load) или
    # сломанный агентский config.yml (build_agent_config_for_server → load_config)
    # иначе роняли бы импорт server голым traceback'ом ещё до старта uvicorn —
    # тот же приём, что config_modules/env_vars.py уже применяет к битым
    # GC_*-переменным окружения.
    SERVER_CONFIG: Dict[str, Any] = load_server_config()
    BASE_AGENT_CONFIG: Dict[str, Any] = build_agent_config_for_server(SERVER_CONFIG)
except (ValueError, OSError, yaml.YAMLError) as exc:
    raise SystemExit(f"[gc] сервер не стартовал: конфиг не загружается ({exc})") from exc
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
    global _swarm_monitor

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
            except OSError:
                pass

    # 2. Grace period — CLI пытается дописать summary.
    await asyncio.sleep(3.0)

    # 3. Добиваем оставшихся + обновляем meta.
    for swarm_id, pid in pids_to_kill:
        if swarm_lifecycle._is_pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
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
                        threads_dir,
                        swarm_id,
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
            detail={"status": "error", "payload": _ERR_SYSTEM_AND_TASK_ID},
        )

    # Бронь и прогон — одним снимком реестра, бронь первой: пока строится
    # контекст, нового прогона ещё нет, а под тем же ключом лежит прошлый
    # завершённый с TTL 15 минут. Клиент, которому только что ответили
    # `queued`, иначе видел 404 или `completed` с результатом прошлого прогона.
    building_since, run = agent_run_registry.get_status_snapshot(system=sys_norm, task_id=task_norm)
    if building_since is not None:
        return {
            "status": "running",
            "system": sys_norm,
            "task_id": task_norm,
            "thread_id": task_norm,
            # Прогона ещё нет, поэтому оба времени — от брони. С null зависшая
            # сборка для сторожа по `last_activity_at` не старела никогда.
            "started_at": building_since,
            "last_activity_at": building_since,
        }
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

    if run.cancelled:
        # Снятый прогон — не упавший. Так выглядит остановка uvicorn и явная
        # отмена таски; без отдельного статуса клиент видел `completed` с
        # пустым результатом и не мог отличить одно от другого.
        return {
            "status": "cancelled",
            "system": sys_norm,
            "task_id": task_norm,
            "thread_id": run.thread_id,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
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


#: Потолок ожидания чужой сборки контекста по умолчанию, секунд.
_CONTEXT_BUILD_WAIT_DEFAULT_S = 120.0


def _context_build_wait_s(raw: Any) -> float:
    """Потолок ожидания чужой сборки из `server.context_build_wait_seconds`.

    Не задан — 120 с: с запасом на законно медленную сборку (MCP-дискавери
    идёт по сети), но конечный. Мусор — не число, ноль, отрицательное,
    бесконечность — не отключает потолок и не роняет старт: дефолт и
    предупреждение, как у `log_level`. Строку принимаем: из `${ENV:...}`
    значение приезжает строкой.
    """
    if raw is None:
        return _CONTEXT_BUILD_WAIT_DEFAULT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float("nan")
    # `bool` — подкласс `int`: `true` в YAML иначе стал бы потолком в секунду.
    # NaN отсекается тем же сравнением — с ним оно ложно.
    if isinstance(raw, bool) or not 0 < value < float("inf"):
        logger.warning(
            "server.context_build_wait_seconds=%r — нужно положительное конечное число секунд, беру %g",
            raw,
            _CONTEXT_BUILD_WAIT_DEFAULT_S,
        )
        return _CONTEXT_BUILD_WAIT_DEFAULT_S
    return value


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

    try:
        task_id = ensure_task_id(payload)
    except ValueError as e:
        # Присланный task_id уезжает в имя каталога и дальше в пути ФС.
        # Отвергаем ДО любого побочного эффекта, а не падаем позже в сторе.
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": f"invalid task_id: {e}"},
        )

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

    # Путь проходится заново после каждой чужой сборки контекста: пока она
    # шла, прогон мог стартовать — тогда сообщение уходит в него, — а мог и не
    # стартовать, если сборка упала; тогда строим сами. Откладывать сообщение
    # «до следующего вебхука» нельзя: следующего может не быть вовсе.
    #
    # Потолок — на всё ожидание запроса, а не на каждую чужую сборку: после
    # отказа одной ключ может занять другой ждущий, и потолки по сборке
    # складывались бы, а HTTP-запрос висел бы открытым их сумму. Ставится при
    # первом ожидании: запрос без чужой сборки конфиг и не читает.
    ceiling: Optional[float] = None
    deadline = 0.0
    while True:
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

        # Ключ занимаем ДО первого await: сборка контекста ниже идёт с
        # дискавери MCP по сети, и без брони второй вебхук на ту же задачу
        # успевал проскочить проверку занятости выше.
        if agent_run_registry.reserve(system=system, task_id=task_id):
            break
        # Контекст этой задачи строит другой вебхук — ждём конца его сборки, но
        # не дольше потолка. Своего дедлайна у сборки нет, и вводить его не
        # станем: медленная, но законная сборка не должна падать. Без потолка же
        # ждущий висел с открытым запросом всё время сборки, а если прогон
        # короче сборки, одновременные вебхуки строили контекст по очереди —
        # источник с ретраем по таймауту получал шторм на ту же задачу.
        # По истечении — честный 503: сообщение не принято, повторять клиенту.
        if ceiling is None:
            ceiling = _context_build_wait_s((SERVER_CONFIG.get("server") or {}).get("context_build_wait_seconds"))
            deadline = asyncio.get_running_loop().time() + ceiling
        try:
            async with asyncio.timeout_at(deadline):
                await agent_run_registry.await_build(system=system, task_id=task_id)
        except TimeoutError:
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
            )

    # Всё, что может упасть после взятия брони, — внутри `try`: бронь, не
    # снятая из-за исключения до него, навсегда усыпила бы вебхуки задачи.
    try:
        config_dict = dict(BASE_AGENT_CONFIG)
        if trace_id:
            # Shallow copy достаточно: значение — скаляр. Мутировать
            # BASE_AGENT_CONFIG нельзя, он процесс-глобальный.
            config_dict["trace_id"] = trace_id
        auth_connector = build_auth_connector(config_dict)
        # Легаси-форс (спека §2): agent_overrides.model/completions_url поверх
        # непустого реестра моделей — тот же контракт, что CLI --model
        # (agent_overrides перекрывают config.yml так же, как CLI-аргументы).
        force_legacy = cli_forces_legacy_model(config_dict)
        if force_legacy:
            logger.warning("agent_overrides: уровни отключены на этот запуск (легаси-форс)")
        # Асинхронная сборка: внутри MCP-дискавери — сетевые вызовы к каждому
        # серверу с общим дедлайном. Синхронный вариант морозил loop uvicorn на
        # всё это время, а с переездом прогоны стали тасками на общем loop,
        # поэтому заморозка била разом по всем эндпоинтам и всем идущим прогонам.
        ctx = await acreate_run_context(config_dict, auth_connector=auth_connector, force_legacy=force_legacy)

        await agent_run_registry.astart_if_absent(
            system=system,
            task_id=task_id,
            executor_factory=AgentExecutor,
            ctx=ctx,
            initial_user_message=message_str,
            thread_id=task_id,
            verbose=None,
        )
    finally:
        # Снять бронь в ЛЮБОМ исходе — это и будит ждущих. Прогон стартовал —
        # они уйдут в него; сборка упала или её сняли — первый из них построит
        # свой контекст. Без снятия ключ навсегда остался бы «строящимся».
        agent_run_registry.release_reservation(system=system, task_id=task_id)

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
    except Exception as e:
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
    except Exception as e:
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
            with open(meta_path, encoding="utf-8") as f:
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
    try:
        task_id = ensure_task_id(payload)
    except ValueError as e:
        # Присланный task_id уезжает в имя каталога и дальше в пути ФС.
        # Отвергаем ДО любого побочного эффекта, а не падаем позже в сторе.
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": f"invalid task_id: {e}"},
        )
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
    # `system` тоже уезжает в имя каталога `swarm_<system>_<task_id>`, а
    # `normalize_system` делает лишь strip+lower — `../` проходил насквозь.
    # Здесь коэрсинг, а не отказ: в имени системы законны пробелы и регистр,
    # 400 сломал бы живых клиентов. У `task_id` наоборот отказ — коэрсинг
    # молча склеил бы разные задачи в один тред.
    system = sanitize_system_id(normalize_system(source_value, default_system=default_system))
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
        except Exception as e:
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

    # `task_id` из URL уезжает в путь `threads_dir/swarm_<sys>_<task>/meta.json`.
    # Один strip() пропускал `../`, и обычный GET читал произвольный файл. Разбор
    # тот же, что у соседнего `/logs/{system}/{task_id}`; `system` не отвергаем, а
    # нормализуем — там законны пробелы и регистр.
    # Пустоту проверяем ДО санитайзера: он на пустой строке кидает ValueError,
    # а голый ValueError из хендлера — это 500, тогда как контракт эндпоинта
    # (и тест на `%20`) обещает 400.
    if not str(system or "").strip() or not str(task_id or "").strip():
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": _ERR_SYSTEM_AND_TASK_ID},
        )
    sys_norm = sanitize_system_id(str(system).lower().strip())
    try:
        task_norm = validate_thread_id(str(task_id))
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": str(e)},
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
        threads_dir,
        sandbox_root_dir,
        swarm_id,
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

    # `task_id` из URL уезжает в путь `threads_dir/swarm_<sys>_<task>/meta.json`.
    # Один strip() пропускал `../`, и обычный GET читал произвольный файл. Разбор
    # тот же, что у соседнего `/logs/{system}/{task_id}`; `system` не отвергаем, а
    # нормализуем — там законны пробелы и регистр.
    # Пустоту проверяем ДО санитайзера: он на пустой строке кидает ValueError,
    # а голый ValueError из хендлера — это 500, тогда как контракт эндпоинта
    # (и тест на `%20`) обещает 400.
    if not str(system or "").strip() or not str(task_id or "").strip():
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": _ERR_SYSTEM_AND_TASK_ID},
        )
    sys_norm = sanitize_system_id(str(system).lower().strip())
    try:
        task_norm = validate_thread_id(str(task_id))
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "payload": str(e)},
        )

    swarm_id = f"swarm_{sys_norm}_{task_norm}"
    threads_dir = str(SWARM_CONFIG["threads_dir"])
    sandbox_root_dir = str(SWARM_CONFIG.get("sandbox_root_dir", ""))
    meta = swarm_storage.load_swarm_meta(threads_dir, swarm_id)
    # Канал E (шаг 1.9): messages.jsonl лежит внутри sandbox-каталога.
    messages = swarm_storage.load_swarm_messages(threads_dir, swarm_id, sandbox_root_dir)
    stdout_lines, stderr_lines = swarm_storage.load_swarm_stdout_stderr(threads_dir, swarm_id)

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


#: Уровни, которые понимает uvicorn. `logging` знает и другие написания
#: (`WARN`, `FATAL`), а uvicorn делает по значению прямой `LOG_LEVELS[level]` и
#: падает KeyError. До этой ветки `server.log_level` читался и НИКУДА не
#: передавался, поэтому конфиг с `warn` работал; передав его в uvicorn, мы бы
#: уронили такие установки на старте.
_UVICORN_LOG_LEVELS = frozenset({"critical", "error", "warning", "info", "debug", "trace"})

#: Написания, которые logging принимает как синонимы, а uvicorn — нет.
_LOG_LEVEL_ALIASES = {"warn": "warning", "fatal": "critical"}


def _normalize_log_level(raw) -> str:
    """Привести `server.log_level` к тому, что примет uvicorn.

    Неизвестное значение не роняет старт, а даёт `info` и предупреждение:
    поднятый сервер с неожиданной подробностью логов лучше, чем упавший.
    """
    level = str(raw if raw is not None else "info").strip().lower()
    level = _LOG_LEVEL_ALIASES.get(level, level)
    if level not in _UVICORN_LOG_LEVELS:
        logger.warning(
            "server.log_level=%r uvicorn не понимает (допустимы: %s) — беру info",
            raw,
            ", ".join(sorted(_UVICORN_LOG_LEVELS)),
        )
        return "info"
    return level


def serve() -> int:
    """Поднять REST-сервер по config_server.yml. Тело точки входа gc-serve."""
    server_cfg = SERVER_CONFIG.get("server", {}) or {}
    host = str(server_cfg.get("host", "0.0.0.0"))
    port_raw = server_cfg.get("port", 8000)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 8000

    # basicConfig ровно здесь, а не на импорте модуля: gc_server импортируют и
    # как библиотеку (cleanup_threads, тесты), и настраивать чужое логирование
    # за них нельзя. Без этого вызова наши logger.info/warning не доходили
    # никуда: uvicorn поднимает только СВОИ логгеры, а до stderr через
    # logging.lastResort пролезал лишь WARNING+. То есть строка «читаю такой-то
    # config_server.yml» существовала, но в проде её не видел никто.
    log_level = _normalize_log_level(server_cfg.get("log_level"))
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Какой конфиг прочитан — печатаем ЗДЕСЬ, а не в резолвере. `SERVER_CONFIG`
    # вычисляется на уровне модуля, то есть ДО того, как `serve()` дойдёт до
    # `basicConfig` выше: строка из резолвера уходила через `logging.lastResort`
    # с порогом WARNING и не доходила никуда. Проверено живым `gc-serve` —
    # конфиг был найден, а строки в выводе не было. Всё нужное уже лежит в
    # `SERVER_CONFIG["config_path"]`, читать файл заново не требуется.
    logger.info(
        "config_server.yml: %s",
        SERVER_CONFIG.get("config_path") or "не найден, взят минимальный дефолт",
    )

    # Тревога по СОСТОЯНИЮ, а не по факту «файл не найден». Прежде она жила в
    # резолвере конфига и срабатывала только когда файла нет нигде — а найденный
    # конфиг с пустым auth_token молчал, хотя это ровно тот же незащищённый
    # сервер. Именно туда ведёт подхват постороннего config_server.yml.
    if not server_cfg.get("auth_token"):
        logger.warning(
            "сервер стартует БЕЗ авторизации: auth_token не задан (конфиг: %s). "
            "Любой, кто дотянется до %s:%s, получит доступ к агенту.",
            SERVER_CONFIG.get("config_path") or "минимальный дефолт",
            host,
            port,
        )

    # Было "src.server:app": под установленным колесом (sources = ["src"]) модуль
    # называется server, поэтому строка резолвилась только при запуске из корня
    # репозитория, а из wheel падала ModuleNotFoundError.
    # log_level передаётся явно — server.log_level из конфига читался и не
    # использовался нигде.
    uvicorn.run("gc_server.server:app", host=host, port=port, log_level=log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
