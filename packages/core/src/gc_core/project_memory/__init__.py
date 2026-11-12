from __future__ import annotations

from pathlib import Path
from typing import Any

from gc_core.project_memory.compaction import (
    SOFT_APPEND_INPUT_LIMIT_CHARS,
    CompactionResult,
    acompact_text,
)
from gc_core.project_memory.status import (
    aattach_status_to_prompt_vars,
    build_project_memory_status,
    build_project_memory_status_for_cwd,
    fill_percent,
)
from gc_core.project_memory.storage import (
    DEFAULT_MAX_CHARS,
    MAX_CHARS,
    ProjectMemoryError,
    ProjectMemoryStat,
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
        from gc_core.config_modules.env_vars import project_gc_dir

        gc_dir = project_gc_dir()
    return Path(gc_dir) / _PROJECT_MEMORY_FILE


def project_of(ctx: Any | None = None):
    """Проект памяти: каталог ``ctx.project_gc_dir`` или текущий каталог."""
    import os

    from gc_core.agent_store import project_key_for, project_key_of

    key = project_key_of(ctx) if ctx is not None else None
    if key is not None:
        return key
    return project_key_for(os.getcwd(), ctx)


async def aget_hash(ctx: Any | None = None) -> str | None:
    """Версия long memory для baseline/changed hints (D10, D40): sha256
    текста, ``None`` — памяти нет, она пуста или не читается."""
    from gc_core.agent_store import agent_store_of

    try:
        memory = await agent_store_of(ctx).read_memory(project_of(ctx))
    except (OSError, UnicodeDecodeError):
        return None
    return memory.version if memory is not None else None


__all__ = [
    "DEFAULT_MAX_CHARS",
    "MAX_CHARS",
    "SOFT_APPEND_INPUT_LIMIT_CHARS",
    "CompactionResult",
    "ProjectMemoryError",
    "ProjectMemoryStat",
    "aattach_status_to_prompt_vars",
    "acompact_text",
    "aget_hash",
    "append",
    "build_project_memory_status",
    "build_project_memory_status_for_cwd",
    "content_hash",
    "fill_percent",
    "project_of",
    "read",
    "resolve_project_memory_path",
    "stat",
    "write",
]
