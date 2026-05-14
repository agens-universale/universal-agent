import os
from typing import Any, Dict

from config_modules import env_vars
from src.rest_api.server_config import PROJECT_ROOT


def _resolve_path(maybe_path: Any, default_rel: str) -> str:
    """
    Преобразовать конфиг-путь в абсолютный.

    - Если значение пустое или не строка — использовать PROJECT_ROOT/default_rel.
    - Если путь абсолютный — вернуть как есть.
    - Если путь относительный — резолвится от PROJECT_ROOT.
    """
    if not isinstance(maybe_path, str) or not maybe_path.strip():
        return os.path.join(PROJECT_ROOT, default_rel)

    path = maybe_path.strip()
    if os.path.isabs(path):
        return path
    return os.path.join(PROJECT_ROOT, path)


def build_swarm_config(server_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Построить нормализованный конфиг для swarm-режима.

    На вход подаётся целиком SERVER_CONFIG (как в src.server),
    на выходе — словарь с полями:
      - enabled: bool
      - threads_dir: str (абсолютный путь)
      - sandbox_root_dir: str (абсолютный путь)
      - max_concurrent: int
      - ttl_seconds: int
      - limits: Dict[str, int]
      - cli_command: str
      - wrapper_path: str (абсолютный путь)
      - sandbox_tool: str
      - agentmd_path: str (абсолютный путь)
      - skills_dir: str (абсолютный путь)
    """
    raw_swarm = server_cfg.get("swarm") or {}

    enabled = bool(raw_swarm.get("enabled", False))

    paths = env_vars.gc_paths()
    slug = env_vars.project_slug()
    swarm_root = paths["state"] / "projects" / slug / "swarm"
    threads_dir = _resolve_path(
        raw_swarm.get("threads_dir"), str(swarm_root / "threads")
    )
    sandbox_root_dir = _resolve_path(
        raw_swarm.get("sandbox_root_dir"), str(swarm_root / "sandboxes")
    )

    max_concurrent = int(raw_swarm.get("max_concurrent", 10))
    ttl_seconds = int(raw_swarm.get("ttl_seconds", 86400))

    limits_raw = raw_swarm.get("limits") or {}
    limits = {
        "memory_mb": int(limits_raw.get("memory_mb", 1024)),
        "disk_mb": int(limits_raw.get("disk_mb", 2048)),
        "cpu_percent": int(limits_raw.get("cpu_percent", 50)),
        "timeout_seconds": int(limits_raw.get("timeout_seconds", 1800)),
    }

    cli_command = str(raw_swarm.get("cli_command", "gc-cli"))
    wrapper_path = _resolve_path(
        raw_swarm.get("wrapper_path"), "scripts/run-cli-in-sandbox.sh"
    )
    sandbox_tool = str(raw_swarm.get("sandbox_tool", "bubblewrap"))
    agentmd_path = _resolve_path(raw_swarm.get("agentmd_path"), "swarm_agent.md")
    skills_dir = _resolve_path(raw_swarm.get("skills_dir"), "swarm_skills")

    return {
        "enabled": enabled,
        "threads_dir": threads_dir,
        "sandbox_root_dir": sandbox_root_dir,
        "max_concurrent": max_concurrent,
        "ttl_seconds": ttl_seconds,
        "limits": limits,
        "cli_command": cli_command,
        "wrapper_path": wrapper_path,
        "sandbox_tool": sandbox_tool,
        "agentmd_path": agentmd_path,
        "skills_dir": skills_dir,
    }


__all__ = ["build_swarm_config"]

