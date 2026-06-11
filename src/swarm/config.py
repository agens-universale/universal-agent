import os
from typing import Any, Dict

from config_modules import env_vars
from rest_api.server_config import PROJECT_ROOT


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


def _resolve_optional(maybe_path: Any) -> str:
    """Опциональный путь: абсолютный → как есть; относительный → от PROJECT_ROOT;
    пустой/не-строка → "" (пусто = «используй дефолт»)."""
    if not isinstance(maybe_path, str) or not maybe_path.strip():
        return ""
    path = maybe_path.strip()
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


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
    # Процессный каталог sworm'а (meta/completion/stdout/stderr). Поле в конфиге —
    # `swarm_threads_dir` (старое имя `threads_dir` ещё читается для совместимости).
    threads_dir = _resolve_path(
        raw_swarm.get("swarm_threads_dir", raw_swarm.get("threads_dir")),
        str(swarm_root / "threads"),
    )
    sandbox_root_dir = _resolve_path(
        raw_swarm.get("sandbox_root_dir"), str(swarm_root / "sandboxes")
    )

    # Куда на хосте lifecycle сохраняет агентские .gc/logs из песочницы при
    # финализации. Абсолютный путь — как есть; относительный — от PROJECT_ROOT;
    # пусто → дефолт `threads_dir/<swarm_id>/agent_logs/`.
    swarm_logs_dir = _resolve_optional(raw_swarm.get("swarm_logs_dir"))

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
    agentmd_path = _resolve_path(raw_swarm.get("agentmd_path"), "prompts/server_agent.md")
    skills_dir = _resolve_path(raw_swarm.get("skills_dir"), "swarm_skills")

    # Шаг 1.11 (AR-6, AR-7): bind-mount пути для wrapper.sh.
    # cli_venv_root и agent_dep_dir принимают только абсолютные пути;
    # если не заданы — wrapper делает fallback на legacy-поведение (PATH lookup).
    cli_venv_root = _abs_or_empty(raw_swarm.get("cli_venv_root"))
    agent_dep_dir = _abs_or_empty(raw_swarm.get("agent_dep_dir"))
    extra_mounts_raw = raw_swarm.get("extra_mounts") or []
    if not isinstance(extra_mounts_raw, list):
        extra_mounts_raw = []
    extra_mounts = [
        str(p).strip() for p in extra_mounts_raw
        if isinstance(p, str) and p.strip()
    ]

    # agent_overrides пробрасываем из server_cfg как есть — materializer применит
    # их к raw YAML с auto-wrap'ом flat → {agent: ...}, чтобы единая секция
    # config_server.yml::agent_overrides работала и для webhook'а (cfg.update
    # на flat-dict) и для swarm'а (deep_merge в raw YAML).
    agent_overrides_raw = server_cfg.get("agent_overrides") or {}
    agent_overrides = (
        dict(agent_overrides_raw) if isinstance(agent_overrides_raw, dict) else {}
    )

    return {
        "enabled": enabled,
        "threads_dir": threads_dir,
        "sandbox_root_dir": sandbox_root_dir,
        "swarm_logs_dir": swarm_logs_dir,
        "max_concurrent": max_concurrent,
        "ttl_seconds": ttl_seconds,
        "limits": limits,
        "cli_command": cli_command,
        "wrapper_path": wrapper_path,
        "sandbox_tool": sandbox_tool,
        "agentmd_path": agentmd_path,
        "skills_dir": skills_dir,
        "cli_venv_root": cli_venv_root,
        "agent_dep_dir": agent_dep_dir,
        "extra_mounts": extra_mounts,
        "agent_overrides": agent_overrides,
    }


def _abs_or_empty(value: Any) -> str:
    """Принять только непустую строку с абсолютным путём; иначе вернуть ''."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate:
        return ""
    if not os.path.isabs(candidate):
        return ""
    return candidate


__all__ = ["build_swarm_config"]

