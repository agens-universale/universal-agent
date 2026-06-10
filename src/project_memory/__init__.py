from __future__ import annotations

from pathlib import Path
from typing import Any

from project_memory.compaction import (
    CompactionResult,
    SOFT_APPEND_INPUT_LIMIT_CHARS,
    compact_text,
)
from project_memory.status import (
    attach_status_to_prompt_vars,
    build_project_memory_status,
    build_project_memory_status_for_cwd,
    fill_percent,
)
from project_memory.storage import (
    ProjectMemoryError,
    ProjectMemoryStat,
    DEFAULT_MAX_CHARS,
    MAX_CHARS,
    append,
    content_hash,
    read,
    stat,
    write,
)

_PROJECT_MEMORY_FILE = "project_memory.md"


def resolve_project_memory_path(ctx: Any | None = None) -> Path:
    """Путь к project long memory; ``ctx.project_gc_dir`` или ``project_gc_dir()``."""
    gc_dir = getattr(ctx, "project_gc_dir", None) if ctx is not None else None
    if gc_dir is None:
        from config_modules.env_vars import project_gc_dir

        gc_dir = project_gc_dir()
    return Path(gc_dir) / _PROJECT_MEMORY_FILE


def get_hash(ctx: Any | None = None) -> str | None:
    """Хеш содержимого long memory для baseline/changed hints (D10, D40)."""
    return content_hash(resolve_project_memory_path(ctx))

__all__ = [
    "CompactionResult",
    "ProjectMemoryError",
    "ProjectMemoryStat",
    "DEFAULT_MAX_CHARS",
    "MAX_CHARS",
    "SOFT_APPEND_INPUT_LIMIT_CHARS",
    "append",
    "attach_status_to_prompt_vars",
    "content_hash",
    "get_hash",
    "build_project_memory_status",
    "build_project_memory_status_for_cwd",
    "fill_percent",
    "compact_text",
    "read",
    "resolve_project_memory_path",
    "stat",
    "write",
]
