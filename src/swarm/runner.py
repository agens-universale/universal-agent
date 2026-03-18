import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from . import storage


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_swarm_id(system: str, task_id: str) -> str:
    system_norm = str(system or "").strip().lower()
    task_norm = str(task_id or "").strip()
    return f"{system_norm}_{task_norm}"


def start_swarm_process(
    swarm_cfg: Dict[str, Any],
    *,
    system: str,
    task_id: str,
    payload: Dict[str, Any],
) -> Tuple[str, int]:
    """
    Запустить обёрточный скрипт для сворм-режима.

    Возвращает (swarm_id, pid).
    """
    swarm_id = _make_swarm_id(system, task_id)
    threads_dir: str = swarm_cfg["threads_dir"]
    sandbox_root_dir: str = swarm_cfg["sandbox_root_dir"]
    limits: Dict[str, Any] = swarm_cfg["limits"]

    # Директория песочницы для этого сворма.
    sandbox_dir = os.path.join(sandbox_root_dir, swarm_id)
    os.makedirs(sandbox_dir, exist_ok=True)

    # Базовая метадата (совместимая с обычным meta.json) + swarm_meta.
    base_meta: Dict[str, Any] = {
        "system": system,
        "task_id": task_id,
    }
    swarm_meta_patch: Dict[str, Any] = {
        "id": swarm_id,
        "status": "queued",
        "created_at": _utc_now_iso(),
        "limits": limits,
        "sandbox_path": sandbox_dir,
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
            # Пути до swarm-специфичных инструкций и директории со скиллами.
            "SWARM_AGENTMD_PATH": agentmd_path,
            "SWARM_SKILLS_DIR": skills_dir,
        }
    )

    # Для MVP предполагаем, что wrapper_path существует и исполняем.
    # Ошибки запуска прокидываем выше.
    proc = subprocess.Popen(  # noqa: S603
        [wrapper_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
    )

    # Обновляем статус на running и сохраняем PID.
    swarm_meta_patch.update(
        {
            "status": "running",
            "started_at": _utc_now_iso(),
            "pid": proc.pid,
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

