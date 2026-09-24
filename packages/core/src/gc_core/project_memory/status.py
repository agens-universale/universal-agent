"""Project Info string builder for long memory.

Pure function — no side effects, no knowledge of ctx/state.
Intentionally never reveals the file path, the tool name, or any internal mechanics
to the model. The agent learns *that* a memory exists and *roughly how big it is*;
*how to access it* is conveyed via the tool registry, not via this string.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from gc_core.config_modules.project_memory import (
    DEFAULT_PROJECT_MEMORY_MAX_CHARS,
    project_memory_limits_from_config_dict,
)
from gc_core.constants.tool_names import PROJECT_MEMORY_TOOL_NAME
from gc_core.project_memory import storage

_FILE_NAME = "project_memory.md"


def fill_percent(length: int, max_chars: int) -> int:
    if max_chars <= 0:
        return 0
    return min(100, round(length * 100 / max_chars))


_EMPTY_STATUS = (
    "project doesn't have long memory yet. "
    "When you discover or verify a durable fact (goal, requirement, convention, "
    "stable decision, reference fact, user preference), use the appropriate memory tool "
    "with action=append/write to persist it for future sessions."
)


def build_project_memory_status_for_cwd(
    cwd: Optional[str] = None,
    *,
    config_dict: dict | None = None,
) -> str:
    """Resolve the project-local memory path and build the status string.

    Callers that have no business knowing where memory lives use this entry
    point — internal file name and directory layout stay encapsulated.
    """
    from gc_core.config_modules.env_vars import project_gc_dir

    limits = project_memory_limits_from_config_dict(config_dict)
    return build_project_memory_status(
        project_gc_dir(cwd) / _FILE_NAME,
        max_chars=limits["max_chars"],
    )


def attach_status_to_prompt_vars(
    prompt_vars: dict,
    cwd: Optional[str] = None,
    *,
    config_dict: dict | None = None,
) -> None:
    """Add `longMemoryStatus` to the given prompt-variables dict.

    Не добавляет статус, если ``project_memory`` в ``stop_tools`` (D36).
    """
    stop = (config_dict or {}).get("stop_tools")
    if isinstance(stop, list) and PROJECT_MEMORY_TOOL_NAME in stop:
        return
    try:
        prompt_vars["longMemoryStatus"] = build_project_memory_status_for_cwd(
            cwd, config_dict=config_dict
        )
    except Exception:  # pragma: no cover — memory must never break metadata
        return


def build_project_memory_status(
    path: Path,
    *,
    max_chars: int = DEFAULT_PROJECT_MEMORY_MAX_CHARS,
) -> str:
    """Return the multi-line status injected into Project Info as longMemoryStatus.

    Two outputs by file state:
      - missing/empty/whitespace-only → empty-status nudging the model to persist
        durable facts when encountered.
      - populated → size hint only; read when conventions/decisions may matter (D16).
    """
    info = storage.stat(path)
    if not info.exists or info.chars == 0:
        return f"{_EMPTY_STATUS}\nLimit: {max_chars} chars (0% filled)."
    text = storage.read(path)
    if not text.strip():
        return f"{_EMPTY_STATUS}\nLimit: {max_chars} chars (0% filled)."
    usage_pct = fill_percent(info.chars, max_chars)
    return (
        f"project has long memory — {info.chars}/{max_chars} chars "
        f"({usage_pct}% filled), {info.lines} lines.\n"
        "Use the appropriate memory tool with action=read when standing goals, rules, "
        "decisions, or durable facts may affect this task — not on every turn."
    )
