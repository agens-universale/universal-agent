"""Project Info string builder for long memory.

Pure function — no side effects, no knowledge of ctx/state.
Intentionally never reveals the file path, the tool name, or any internal mechanics
to the model. The agent learns *that* a memory exists and *roughly how big it is*;
*how to access it* is conveyed via the tool registry, not via this string.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from gc_core.config_modules.project_memory import (
    DEFAULT_PROJECT_MEMORY_MAX_CHARS,
)
from gc_core.constants.tool_names import PROJECT_MEMORY_TOOL_NAME
from gc_core.project_memory import storage
from gc_core.settings import settings_of

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
    settings: Any = None,
) -> str:
    """Resolve the project-local memory path and build the status string.

    ``settings`` — то, что принимает ``settings_of`` (ctx, ``LoadedSettings``,
    ``AppConfig``); ``None`` — умолчания.

    Callers that have no business knowing where memory lives use this entry
    point — internal file name and directory layout stay encapsulated.
    """
    from gc_core.config_modules.env_vars import project_gc_dir

    return build_project_memory_status(
        project_gc_dir(cwd) / _FILE_NAME,
        max_chars=settings_of(settings).project_memory.max_chars,
    )


async def aattach_status_to_prompt_vars(
    prompt_vars: dict,
    *,
    store: Any,
    project: Any,
    settings: Any = None,
) -> None:
    """Add `longMemoryStatus` to the given prompt-variables dict.

    Не добавляет статус, если ``project_memory`` в ``stop_tools`` (D36).
    ``settings`` — то, что принимает ``settings_of``; ``None`` — умолчания.
    Память читает хранилище агента ``store`` (проект ``project``).
    """
    settings = settings_of(settings)
    if PROJECT_MEMORY_TOOL_NAME in settings.agent.stop_tools:
        return
    try:
        try:
            memory = await store.read_memory(project)
        except (OSError, UnicodeDecodeError):
            memory = None  # не читается — как нет (как раньше)
        prompt_vars["longMemoryStatus"] = status_of_text(
            memory.text if memory is not None else "",
            max_chars=settings.project_memory.max_chars,
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
    text = storage.read(path) if info.exists else ""
    return status_of_text(text, max_chars=max_chars)


def status_of_text(
    text: str, *, max_chars: int = DEFAULT_PROJECT_MEMORY_MAX_CHARS
) -> str:
    """Статус по тексту памяти (пустой, из пробелов или нет — «памяти нет»)."""
    if not text.strip():
        return f"{_EMPTY_STATUS}\nLimit: {max_chars} chars (0% filled)."
    chars, lines = len(text), _count_lines(text)
    usage_pct = fill_percent(chars, max_chars)
    return (
        f"project has long memory — {chars}/{max_chars} chars "
        f"({usage_pct}% filled), {lines} lines.\n"
        "Use the appropriate memory tool with action=read when standing goals, rules, "
        "decisions, or durable facts may affect this task — not on every turn."
    )


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)
