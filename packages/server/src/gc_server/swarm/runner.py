import copy
import json
import logging
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import yaml
from gc_core.config import resolve_base_config_path
from gc_core.config_modules import env_vars
from gc_core.config_modules.compat import map_legacy_path
from gc_core.config_modules.merge import deep_merge
from gc_core.config_modules.path_hints import check_setting_path
from gc_core.settings import (
    _replace_at,
    check_registry_pairs,
    expand_section_updates,
)
from gc_core.utils.user_text import read_user_text_or_none

from gc_server.swarm import storage as storage

logger = logging.getLogger(__name__)


def _apply_agent_overrides_to_raw(
    raw: Dict[str, Any], overrides: Dict[str, Any]
) -> Dict[str, Any]:
    """``agent_overrides`` поверх сырого YAML с семантикой ``override_settings``."""
    if not isinstance(overrides, dict) or not overrides:
        return raw
    updates = expand_section_updates(
        {str(key): value for key, value in overrides.items()}
    )
    # Каждый раскрытый путь: опечатка внутри секции (``security:
    # {read_gard: …}``) иначе молча легла бы лишним ключом.
    for key in updates:
        mapped = map_legacy_path(key)
        if mapped is not None:
            check_setting_path(mapped)
    # Правило пары адрес+токен — как в CLI и webhook: адрес записи реестра
    # без токена оставил бы токен файла на новом адресе.
    check_registry_pairs(updates)
    result = copy.deepcopy(raw)
    for dotted, value in updates.items():
        _replace_at(result, dotted, copy.deepcopy(value))
    return result


def _apply_swarm_env_to_raw(raw: Dict[str, Any], config_dir: str) -> None:
    """Параллель `config_modules.common.swarm_env_updates` для raw YAML.

    Та функция работает на настройках (`paths.agent_md_path` и т.п. слоем env),
    а в materialize мы оперируем сырой YAML-структурой, где те же поля
    лежат в `paths.*`. Чтобы избежать divergence с обычным CLI'ем — здесь
    делаем такую же логику, но на raw.
    """
    paths_section = raw.get("paths")
    if not isinstance(paths_section, dict):
        paths_section = {}
        raw["paths"] = paths_section

    def _resolve(value: str) -> str:
        if os.path.isabs(value):
            return value
        return os.path.join(config_dir, value)

    if env_vars.env.swarm_agentmd_path is not None:
        paths_section["agent_md_path"] = _resolve(
            str(env_vars.env.swarm_agentmd_path)
        )
    if env_vars.env.swarm_threads_dir is not None:
        paths_section["threads_data_path"] = _resolve(
            str(env_vars.env.swarm_threads_dir)
        )
    if env_vars.env.swarm_skills_dir is not None:
        paths_section["skills_data_path"] = _resolve(
            str(env_vars.env.swarm_skills_dir)
        )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _compute_deadline_iso(timeout_seconds: int) -> str:
    """Вернуть ISO-timestamp когда sworm должен быть принудительно убит по таймауту."""
    return (
        datetime.now(UTC) + timedelta(seconds=int(timeout_seconds))
    ).isoformat()


def _copy_swarm_assets(
    sandbox_path: str,
    swarm_cfg: Dict[str, Any],
    swarm_overrides: Dict[str, Any],
) -> None:
    # Опционально: swarm-specific prompt (agentmd_path) копируется в sandbox.
    swarm_agentmd_path = swarm_cfg.get("agentmd_path") or ""
    if swarm_agentmd_path and os.path.isfile(swarm_agentmd_path):
        dst = os.path.join(sandbox_path, ".gc", "agent.md")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(swarm_agentmd_path, dst)
        swarm_overrides["paths"]["agent_md_path"] = "/work/.gc/agent.md"

    # Опционально: swarm-specific skills (skills_dir) — копируем содержимое.
    # Промах по каталогу молчаливым быть не должен: без копирования
    # paths.skills_data_path не переопределяется, песочница поднимается без
    # скиллов, и sworm завершается «успешно» — ровно так и жил дефолт,
    # указывавший в несуществующий <assets>/swarm_skills.
    swarm_skills_dir = swarm_cfg.get("skills_dir") or ""
    if swarm_skills_dir and os.path.isdir(swarm_skills_dir):
        dst_skills = os.path.join(sandbox_path, ".gc", "skills")
        if os.path.isdir(dst_skills):
            shutil.rmtree(dst_skills)
        shutil.copytree(swarm_skills_dir, dst_skills)
        swarm_overrides["paths"]["skills_data_path"] = "/work/.gc/skills"
    elif swarm_skills_dir and swarm_cfg.get("skills_dir_configured"):
        # Кричим только про путь, который человек выписал руками: это ошибка
        # конфигурации. Несуществующий ДЕФОЛТ означает лишь «swarm-скиллы не
        # заводили» — законное состояние свежей установки, и предупреждать о нём
        # на каждом запуске значит приучить пропускать это предупреждение мимо.
        logger.warning(
            "runtime config materialization: каталог скиллов %s не существует — "
            "в песочницу ничего не скопировано, агент пойдёт БЕЗ swarm-скиллов. "
            "Путь берётся из swarm.skills_dir в config_server.yml "
            "(относительный — от каталога самого конфига).",
            swarm_skills_dir,
        )


def _materialize_runtime_config(
    host_config_path: str,
    sandbox_path: str,
    swarm_cfg: Dict[str, Any],
) -> str:
    """Сгенерировать ${sandbox_path}/.gc/config.yml для CLI внутри bwrap.

    Реализует AR-1 (см. swarm_spec.md §10). Слои merge'а в порядке применения
    (каждый последующий перезаписывает предыдущий через deep_merge):

      1. Base file — raw YAML из host_config_path (XDG / package default
         через resolve_base_config_path, либо явный swarm.host_config_path).
      2. Project overlay — <cwd>/.gc/config.yml относительно CWD сервера,
         если файл существует и отличается от base. Симметрично с тем, как
         load_settings делает layered loading для обычного CLI'я.
      3. SWARM_* env vars — paths.{agent_md_path, threads_data_path,
         skills_data_path}. Параллель с config_modules.common.apply_swarm_env_overrides
         для raw YAML.
      4. agent_overrides — переопределения из config_server.yml::agent_overrides.
         Пути через точку (`agent.stop_tools: [...]`) раскладываются во
         вложенные секции, секции (`agent:`, `paths:` и т.п.) применяются как
         есть. Плоское имя (`stop_tools`) — ошибка с подсказкой пути.
      5. Swarm-specific overrides — ВСЕГДА применяются последними и
         перезаписывают предыдущие слои для критичных sandbox-полей:
           paths.threads_data_path = "/work/.gc/threads"
           paths.logs_dump_dir     = "/work/.gc/logs"
           agent.use_server_subagents = False
         (Опц.) paths.agent_md_path / paths.skills_data_path — на внутренние
         пути после копирования файлов из swarm_cfg.

    Возвращает путь к материализованному файлу на хосте (для логирования/тестов).
    """
    # === Слой 1: base ===
    # «Нет файла» и «файл на месте, но не читается» здесь один исход — пустая база
    # с варнингом. Прежде второй класс уходил наружу: конфиг человека, сохранённый
    # в UTF-16, валил материализацию трейсбеком (`UnicodeDecodeError` — подкласс
    # `ValueError`, а не `OSError`, и мимо `FileNotFoundError` уходил тем более).
    base_text = read_user_text_or_none(host_config_path)
    if base_text is None:
        logger.warning(
            "runtime config materialization: base config %s not found or unreadable, using empty base",
            host_config_path,
        )
        base = {}
        config_dir = os.getcwd()
    else:
        base = yaml.safe_load(base_text) or {}
        config_dir = os.path.dirname(os.path.abspath(host_config_path))

    raw: Dict[str, Any] = base

    # === Слой 2: project overlay <cwd>/.gc/config.yml ===
    # Симметрично с load_settings()'s layered loading. CWD — серверский, тот же,
    # что увидит load_settings() при старте webhook'а. Защитный check
    # `project_path != host_config_path` обходит самомерж когда base file
    # лежит в <cwd>/.gc/ (это бывает, если сервер запущен из проектной директории).
    try:
        project_path = env_vars.project_local_config_path()
    except Exception:
        project_path = None

    if project_path is not None and os.path.abspath(
        project_path
    ) != os.path.abspath(host_config_path):
        project_text = read_user_text_or_none(project_path)
        if project_text is None:
            logger.warning(
                "runtime config materialization: failed to read project overlay %s",
                project_path,
            )
        else:
            raw = deep_merge(raw, yaml.safe_load(project_text) or {})

    # === Слой 3: SWARM_* env vars (raw paths.*) ===
    _apply_swarm_env_to_raw(raw, config_dir)

    # === Слой 4: agent_overrides из config_server.yml ===
    # Как у CLI и webhook: правка заменяет значение по пути (списки сужаются),
    # секция раскладывается на листья.
    agent_overrides = swarm_cfg.get("agent_overrides") or {}
    if agent_overrides:
        raw = _apply_agent_overrides_to_raw(raw, agent_overrides)

    # === Слой 5: swarm-specific overrides (всегда последний, всегда побеждает) ===
    swarm_overrides: Dict[str, Any] = {
        "paths": {
            "threads_data_path": "/work/.gc/threads",
            "logs_dump_dir": "/work/.gc/logs",
        },
        "agent": {
            # Внутри sandbox нет связи с REST-сервером для server-side subagent calls.
            "use_server_subagents": False,
        },
    }

    _copy_swarm_assets(sandbox_path, swarm_cfg, swarm_overrides)

    merged = deep_merge(raw, swarm_overrides)

    gc_dir = os.path.join(sandbox_path, ".gc")
    os.makedirs(gc_dir, exist_ok=True)
    out_path = os.path.join(gc_dir, "config.yml")
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(merged, f, default_flow_style=False, sort_keys=False)
    return out_path


def _resolve_host_config_path(swarm_cfg: Dict[str, Any]) -> Optional[str]:
    """Получить путь к host config.yml для materialize_runtime_config.

    Приоритет:
      1. swarm_cfg["host_config_path"] (если задан явно).
      2. config.resolve_base_config_path() (та же логика что у load_settings).
    """
    explicit = swarm_cfg.get("host_config_path")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    try:
        return resolve_base_config_path()
    except Exception:
        logger.warning("could not resolve host config path", exc_info=True)
        return None


def _make_swarm_id(system: str, task_id: str) -> str:
    # Префикс swarm_ — чтобы swarm-каталоги визуально отличались от обычных
    # тредов вебхука (threads/<sys>_<task> vs threads/swarm_<sys>_<task>).
    system_norm = str(system or "").strip().lower()
    task_norm = str(task_id or "").strip()
    return f"swarm_{system_norm}_{task_norm}"


def _try_materialize_runtime_config(
    swarm_cfg: Dict[str, Any], sandbox_dir: str, swarm_id: str
) -> None:
    host_config_path = _resolve_host_config_path(swarm_cfg)
    if host_config_path:
        try:
            _materialize_runtime_config(
                host_config_path, sandbox_dir, swarm_cfg
            )
        except Exception:
            logger.exception(
                "swarm %s: runtime config materialization failed (proceeding without it)",
                swarm_id,
            )


def start_swarm_process(
    swarm_cfg: Dict[str, Any],
    *,
    system: str,
    task_id: str,
    payload: Dict[str, Any],
    trace_id: Optional[str] = None,
) -> Tuple[str, int]:
    """
    Запустить обёрточный скрипт для сворм-процесса.

    ``trace_id`` — внешний трейс (уже нормализованный вызывающей стороной).
    Уезжает в песочницу как ``SWARM_TRACE_ID``: обёртка добавляет
    ``--trace-id`` в командную строку CLI, только если значение непустое.
    Через конфиг его передать нельзя — ребёнку материализуется сырой YAML, а
    неизвестный верхнеуровневый ключ отвергнет ``extra="forbid"``.

    Возвращает (swarm_id, pid).

    Порядок побочных эффектов: обёртка → песочница → meta(``queued``) →
    ``Popen`` → meta(``running``). Сначала проверяется сама обёртка
    (существование и бит +x), потом создаётся песочница, дальше на диск ложится
    meta в статусе ``queued`` — и только после этого стартует процесс, а meta
    переводится в ``running`` с PID и дедлайном.

    Почему след пишется ДО ``Popen``, а не после: между стартом процесса и
    записью meta есть окно. Упади запись (права, диск) — дерево bwrap+CLI уже
    работает, а на диске о нём нет ничего: monitor не узнает, дедлайн не
    сработает никогда, ``/swarm/status`` пуст, песочница живёт до
    TTL-janitor'а.

    Провал на любом шаге не оставляет ни meta, ни песочницы: и то и другое
    убирается в ``finally``, если создал их этот же вызов. Поэтому повтор с тем
    же ``task_id`` стартует заново, а не упирается в 409 до истечения TTL.
    """
    swarm_id = _make_swarm_id(system, task_id)
    threads_dir: str = swarm_cfg["threads_dir"]
    sandbox_root_dir: str = swarm_cfg["sandbox_root_dir"]
    limits: Dict[str, Any] = swarm_cfg["limits"]
    wrapper_path: str = swarm_cfg["wrapper_path"]

    # Обёртку проверяем ДО первого побочного эффекта. Шаблон config_server.yml
    # пережил переезд scripts/ внутрь пакета со старым значением, относительный
    # путь резолвился в gc_server/swarm/scripts/ — и FileNotFoundError вылезал
    # уже из Popen, когда песочница и meta со статусом queued лежали на диске.
    # Повтор с тем же task_id упирался после этого в 409 на весь ttl_seconds.
    if not os.path.isfile(wrapper_path):
        raise FileNotFoundError(
            f"swarm wrapper не найден: {wrapper_path!r} (поле swarm.wrapper_path "
            "в config_server.yml; относительный путь резолвится от каталога "
            "gc_server/swarm/, пустое значение даёт обёртку рядом с кодом)"
        )
    if not os.access(wrapper_path, os.X_OK):
        raise PermissionError(
            f"swarm wrapper не исполняем: {wrapper_path!r} — нужен бит +x (в колесе обёртка едет как package data)"
        )

    # Директория песочницы для этого сворма.
    sandbox_dir = os.path.join(sandbox_root_dir, swarm_id)
    sandbox_created = not os.path.isdir(sandbox_dir)
    os.makedirs(sandbox_dir, exist_ok=True)

    # Meta со статусом queued пишется ниже, ПЕРЕД Popen, и удаляется в finally,
    # если запуск не состоялся. Так закрыты обе беды сразу: провал запуска не
    # оставляет следа (иначе повтор с тем же task_id получал бы 409
    # swarm_id_already_active до истечения ttl_seconds, сутки по умолчанию), а
    # удачный запуск не может остаться неучтённым, даже если запись статуса
    # running потом не удастся. Единственный читатель — idempotency-проверка в
    # POST /swarm/run, и она идёт под тем же _SWARM_RUN_LOCK, что и запуск.
    proc = None
    meta_written = False
    try:
        # Шаг 1.10 (AR-1): материализуем runtime config.yml для CLI.
        # CLI внутри bwrap не сможет резолвить дефолтные пути config'а — нужно
        # положить его в /work/.gc/config.yml явно. Wrapper передаст --config.
        _try_materialize_runtime_config(swarm_cfg, sandbox_dir, swarm_id)

        # Базовая метадата (совместимая с обычным meta.json) + swarm_meta.
        base_meta: Dict[str, Any] = {
            "system": system,
            "task_id": task_id,
        }
        timeout_seconds = int(limits.get("timeout_seconds", 1800))
        swarm_meta_patch: Dict[str, Any] = {
            "id": swarm_id,
            "created_at": _utc_now_iso(),
            "limits": limits,
            "sandbox_path": sandbox_dir,
            # sandbox жив до TTL janitor'а (см. AR-2 в swarm_spec.md §10).
            "sandbox_status": "active",
        }

        cli_command: str = swarm_cfg["cli_command"]
        sandbox_tool: str = swarm_cfg["sandbox_tool"]
        agentmd_path: str = swarm_cfg.get("agentmd_path", "")
        skills_dir: str = swarm_cfg.get("skills_dir", "")

        # Шаг 1.11 (AR-6, AR-7): bind-mount'ы для venv + Node + extra paths.
        cli_venv_root: str = swarm_cfg.get("cli_venv_root", "") or ""
        agent_dep_dir: str = swarm_cfg.get("agent_dep_dir", "") or ""
        extra_mounts: list = swarm_cfg.get("extra_mounts", []) or []
        # В env передаём как newline-separated строку — wrapper читает построчно.
        extra_mounts_env = "\n".join(
            str(p) for p in extra_mounts if str(p).strip()
        )

        # Передаём базовый контекст через переменные окружения.
        env = os.environ.copy()
        env.update(
            {
                "SWARM_ID": swarm_id,
                "SWARM_SYSTEM": system,
                "SWARM_TASK_ID": task_id,
                "SWARM_THREADS_DIR": threads_dir,
                "SWARM_SANDBOX_DIR": sandbox_root_dir,
                "SWARM_SANDBOX_PATH": sandbox_dir,
                "SWARM_CLI_COMMAND": cli_command,
                "SWARM_SANDBOX_TOOL": sandbox_tool,
                "SWARM_LIMIT_MEMORY_MB": str(limits.get("memory_mb", "")),
                "SWARM_LIMIT_DISK_MB": str(limits.get("disk_mb", "")),
                "SWARM_LIMIT_CPU_PERCENT": str(limits.get("cpu_percent", "")),
                "SWARM_LIMIT_TIMEOUT_SECONDS": str(
                    limits.get("timeout_seconds", "")
                ),
                # Полный payload запроса можем передать в окружение как JSON-строку.
                "SWARM_PAYLOAD_JSON": json.dumps(payload, ensure_ascii=False),
                # Внешний трейс. Пусто = не задан: обёртка тогда не добавляет флаг,
                # и ран в песочнице ведёт себя как раньше (ничего не меняется).
                "SWARM_TRACE_ID": str(trace_id or ""),
                # Пути до swarm-специфичных инструкций и директории со скиллами.
                "SWARM_AGENTMD_PATH": agentmd_path,
                "SWARM_SKILLS_DIR": skills_dir,
                # Bind-mount пути для wrapper.sh (AR-6, AR-7).
                "SWARM_CLI_VENV_ROOT": cli_venv_root,
                "SWARM_AGENT_DEP_DIR": agent_dep_dir,
                "SWARM_EXTRA_MOUNTS": extra_mounts_env,
            }
        )

        # wrapper_path проверен в начале функции; остальные ошибки запуска
        # прокидываем выше — за собой убирает finally ниже.
        # start_new_session=True → PID == PGID, что позволит monitor'у
        # убить ВСЁ дерево процессов (wrapper + bwrap + CLI + дочерние)
        # через os.killpg при превышении таймаута/лимитов или при shutdown.
        # Meta ДО запуска, в статусе queued. Иначе между удачным Popen и записью
        # meta есть окно: если запись упадёт (права, диск), дерево bwrap+CLI уже
        # работает, а на диске о нём нет ничего — monitor не узнает, дедлайн не
        # сработает никогда, /swarm/status пуст, песочница живёт до TTL-janitor.
        # Порядок «сначала след, потом процесс» закрывает именно этот случай.
        storage.save_swarm_meta(
            threads_dir,
            swarm_id,
            base_meta=base_meta,
            swarm_meta_patch={**swarm_meta_patch, "status": "queued"},
        )
        meta_written = True

        proc = subprocess.Popen(
            [wrapper_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            start_new_session=True,
        )
    finally:
        # Убираем за собой ВЕСЬ участок между makedirs и Popen, а не только сам
        # Popen: до запуска здесь ещё материализуется runtime config и собирается
        # env, и их исключение оставило бы на диске каталог, до которого доберётся
        # только TTL-janitor — через сутки. Чужую песочницу (её мог создать
        # предыдущий вызов с тем же swarm_id) не трогаем.
        if proc is None:
            if sandbox_created:
                shutil.rmtree(sandbox_dir, ignore_errors=True)
            # И meta тоже: провалившийся запуск не должен оставлять следа,
            # иначе повтор с тем же task_id упрётся в 409 до истечения
            # ttl_seconds. Идемпотентность сохраняется, учёт возвращается.
            if meta_written:
                storage.delete_swarm_meta(threads_dir, swarm_id)

    # Запуск состоялся: переводим meta из queued в running, с PID и дедлайном.
    swarm_meta_patch.update(
        {
            "status": "running",
            "started_at": _utc_now_iso(),
            "pid": proc.pid,
            "deadline_at": _compute_deadline_iso(timeout_seconds),
        }
    )
    storage.save_swarm_meta(
        threads_dir,
        swarm_id,
        base_meta=base_meta,
        swarm_meta_patch=swarm_meta_patch,
    )

    return swarm_id, int(proc.pid)


__all__ = ["start_swarm_process"]
