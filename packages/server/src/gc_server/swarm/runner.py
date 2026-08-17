import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import yaml

from gc_server.swarm import storage as storage
from gc_core.config import project_local_config_path, resolve_base_config_path
from gc_core.config_modules import env_vars
from gc_core.config_modules.merge import deep_merge


logger = logging.getLogger(__name__)


# Известные top-level секции в config.yml. Если agent_overrides содержит хотя бы
# одну из них на верхнем уровне — считаем его raw-структурой; иначе — flat
# (legacy формат для webhook'а) и оборачиваем в {"agent": ...}.
_KNOWN_RAW_SECTIONS = frozenset({
    "backend", "agent", "agent_auth", "logging", "paths", "mcp",
    "security", "summary", "chat_completions", "working_memory",
    "quality_gate", "tool_usage_notice", "project_memory", "intent_scope",
})


def _normalize_agent_overrides_to_raw(overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Привести agent_overrides к raw-структуре YAML.

    - Если на верхнем уровне есть хотя бы одна известная секция — считаем raw,
      возвращаем как есть.
    - Иначе flat (legacy формат) — оборачиваем в {"agent": ...}. В webhook'е
      `cfg.update(agent_overrides)` работает на flat dict; чтобы тот же
      `agent_overrides: {stop_tools: [...]}` дотянулся до swarm'а через raw
      YAML, нужно положить эти ключи под секцию `agent:`.
    """
    if not isinstance(overrides, dict) or not overrides:
        return {}
    if any(k in _KNOWN_RAW_SECTIONS for k in overrides):
        return dict(overrides)
    return {"agent": dict(overrides)}


def _apply_swarm_env_to_raw(raw: Dict[str, Any], config_dir: str) -> None:
    """Параллель `config_modules.common.apply_swarm_env_overrides` для raw YAML.

    Та функция работает на FLAT dict (`config_dict["agent_md_path"] = ...`),
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
        paths_section["agent_md_path"] = _resolve(str(env_vars.env.swarm_agentmd_path))
    if env_vars.env.swarm_threads_dir is not None:
        paths_section["threads_data_path"] = _resolve(str(env_vars.env.swarm_threads_dir))
    if env_vars.env.swarm_skills_dir is not None:
        paths_section["skills_data_path"] = _resolve(str(env_vars.env.swarm_skills_dir))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_deadline_iso(timeout_seconds: int) -> str:
    """Вернуть ISO-timestamp когда sworm должен быть принудительно убит по таймауту."""
    return (
        datetime.now(timezone.utc) + timedelta(seconds=int(timeout_seconds))
    ).isoformat()


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
         load_config делает layered loading для обычного CLI'я.
      3. SWARM_* env vars — paths.{agent_md_path, threads_data_path,
         skills_data_path}. Параллель с config_modules.common.apply_swarm_env_overrides
         для raw YAML.
      4. agent_overrides — переопределения из config_server.yml::agent_overrides.
         Flat-формат (legacy для webhook'а, типа `stop_tools: [...]`) авто-
         оборачивается в {"agent": ...}. Raw-формат (с секциями `agent:`,
         `paths:` и т.п.) применяется как есть.
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
    try:
        with open(host_config_path, "r", encoding="utf-8") as f:
            base = yaml.safe_load(f) or {}
        config_dir = os.path.dirname(os.path.abspath(host_config_path))
    except FileNotFoundError:
        logger.warning(
            "runtime config materialization: base config %s not found, using empty base",
            host_config_path,
        )
        base = {}
        config_dir = os.getcwd()

    raw: Dict[str, Any] = base

    # === Слой 2: project overlay <cwd>/.gc/config.yml ===
    # Симметрично с load_config()'s layered loading. CWD — серверский, тот же,
    # что увидит load_config() при старте webhook'а. Защитный check
    # `project_path != host_config_path` обходит самомерж когда base file
    # лежит в <cwd>/.gc/ (это бывает, если сервер запущен из проектной директории).
    try:
        project_path = project_local_config_path()
    except Exception:  # noqa: BLE001
        project_path = None

    if (
        project_path is not None
        and os.path.abspath(project_path) != os.path.abspath(host_config_path)
    ):
        try:
            with open(project_path, "r", encoding="utf-8") as f:
                project_raw = yaml.safe_load(f) or {}
            raw = deep_merge(raw, project_raw)
        except OSError:
            logger.warning(
                "runtime config materialization: failed to read project overlay %s",
                project_path,
                exc_info=True,
            )

    # === Слой 3: SWARM_* env vars (raw paths.*) ===
    _apply_swarm_env_to_raw(raw, config_dir)

    # === Слой 4: agent_overrides из config_server.yml ===
    agent_overrides = swarm_cfg.get("agent_overrides") or {}
    if agent_overrides:
        wrapped = _normalize_agent_overrides_to_raw(agent_overrides)
        raw = deep_merge(raw, wrapped)

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

    # Опционально: swarm-specific prompt (agentmd_path) копируется в sandbox.
    swarm_agentmd_path = swarm_cfg.get("agentmd_path") or ""
    if swarm_agentmd_path and os.path.isfile(swarm_agentmd_path):
        dst = os.path.join(sandbox_path, ".gc", "agent.md")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(swarm_agentmd_path, dst)
        swarm_overrides["paths"]["agent_md_path"] = "/work/.gc/agent.md"

    # Опционально: swarm-specific skills (skills_dir) — копируем содержимое.
    swarm_skills_dir = swarm_cfg.get("skills_dir") or ""
    if swarm_skills_dir and os.path.isdir(swarm_skills_dir):
        dst_skills = os.path.join(sandbox_path, ".gc", "skills")
        if os.path.isdir(dst_skills):
            shutil.rmtree(dst_skills)
        shutil.copytree(swarm_skills_dir, dst_skills)
        swarm_overrides["paths"]["skills_data_path"] = "/work/.gc/skills"

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
      2. config.resolve_base_config_path() (та же логика что у load_config).
    """
    explicit = swarm_cfg.get("host_config_path")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    try:
        return resolve_base_config_path()
    except Exception:  # noqa: BLE001
        logger.warning("could not resolve host config path", exc_info=True)
        return None


def _make_swarm_id(system: str, task_id: str) -> str:
    # Префикс swarm_ — чтобы swarm-каталоги визуально отличались от обычных
    # тредов вебхука (threads/<sys>_<task> vs threads/swarm_<sys>_<task>).
    system_norm = str(system or "").strip().lower()
    task_norm = str(task_id or "").strip()
    return f"swarm_{system_norm}_{task_norm}"


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
    """
    swarm_id = _make_swarm_id(system, task_id)
    threads_dir: str = swarm_cfg["threads_dir"]
    sandbox_root_dir: str = swarm_cfg["sandbox_root_dir"]
    limits: Dict[str, Any] = swarm_cfg["limits"]

    # Директория песочницы для этого сворма.
    sandbox_dir = os.path.join(sandbox_root_dir, swarm_id)
    os.makedirs(sandbox_dir, exist_ok=True)

    # Шаг 1.10 (AR-1): материализуем runtime config.yml для CLI.
    # CLI внутри bwrap не сможет резолвить дефолтные пути config'а — нужно
    # положить его в /work/.gc/config.yml явно. Wrapper передаст --config.
    host_config_path = _resolve_host_config_path(swarm_cfg)
    if host_config_path:
        try:
            _materialize_runtime_config(host_config_path, sandbox_dir, swarm_cfg)
        except Exception:  # noqa: BLE001
            logger.exception(
                "swarm %s: runtime config materialization failed (proceeding without it)",
                swarm_id,
            )

    # Базовая метадата (совместимая с обычным meta.json) + swarm_meta.
    base_meta: Dict[str, Any] = {
        "system": system,
        "task_id": task_id,
    }
    timeout_seconds = int(limits.get("timeout_seconds", 1800))
    swarm_meta_patch: Dict[str, Any] = {
        "id": swarm_id,
        "status": "queued",
        "created_at": _utc_now_iso(),
        "limits": limits,
        "sandbox_path": sandbox_dir,
        # sandbox жив до TTL janitor'а (см. AR-2 в swarm_spec.md §10).
        "sandbox_status": "active",
    }
    storage.save_swarm_meta(
        threads_dir,
        swarm_id,
        base_meta=base_meta,
        swarm_meta_patch=swarm_meta_patch,
    )

    wrapper_path: str = swarm_cfg["wrapper_path"]
    cli_command: str = swarm_cfg["cli_command"]
    sandbox_tool: str = swarm_cfg["sandbox_tool"]
    agentmd_path: str = swarm_cfg.get("agentmd_path", "")
    skills_dir: str = swarm_cfg.get("skills_dir", "")

    # Шаг 1.11 (AR-6, AR-7): bind-mount'ы для venv + Node + extra paths.
    cli_venv_root: str = swarm_cfg.get("cli_venv_root", "") or ""
    agent_dep_dir: str = swarm_cfg.get("agent_dep_dir", "") or ""
    extra_mounts: list = swarm_cfg.get("extra_mounts", []) or []
    # В env передаём как newline-separated строку — wrapper читает построчно.
    extra_mounts_env = "\n".join(str(p) for p in extra_mounts if str(p).strip())

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
            "SWARM_LIMIT_TIMEOUT_SECONDS": str(limits.get("timeout_seconds", "")),
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

    # Для MVP предполагаем, что wrapper_path существует и исполняем.
    # Ошибки запуска прокидываем выше.
    # start_new_session=True → PID == PGID, что позволит monitor'у
    # убить ВСЁ дерево процессов (wrapper + bwrap + CLI + дочерние)
    # через os.killpg при превышении таймаута/лимитов или при shutdown.
    proc = subprocess.Popen(  # noqa: S603
        [wrapper_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
        start_new_session=True,
    )

    # Обновляем статус на running и сохраняем PID + deadline.
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

