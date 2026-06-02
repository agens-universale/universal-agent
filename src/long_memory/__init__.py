from __future__ import annotations

from pathlib import Path
from typing import Any

from long_memory.compaction import (
    CompactionResult,
    SOFT_APPEND_INPUT_LIMIT_CHARS,
    compact_text,
)
from long_memory.status import (
    attach_status_to_prompt_vars,
    build_long_memory_status,
    build_long_memory_status_for_cwd,
)
from long_memory.storage import (
    LongMemoryError,
    LongMemoryStat,
    MAX_CHARS,
    append,
    content_hash,
    read,
    stat,
    write,
)

_LONG_MEMORY_FILE = "long_memory.md"


def resolve_long_memory_path(ctx: Any | None = None) -> Path:
    """Путь к project long memory; ``ctx.project_gc_dir`` или ``project_gc_dir()``."""
    gc_dir = getattr(ctx, "project_gc_dir", None) if ctx is not None else None
    if gc_dir is None:
        from config_modules.env_vars import project_gc_dir

        gc_dir = project_gc_dir()
    return Path(gc_dir) / _LONG_MEMORY_FILE


def get_hash(ctx: Any | None = None) -> str | None:
    """Хеш содержимого long memory для baseline/changed hints (D10, D40)."""
    return content_hash(resolve_long_memory_path(ctx))

__all__ = [
    "CompactionResult",
    "LongMemoryError",
    "LongMemoryStat",
    "MAX_CHARS",
    "SOFT_APPEND_INPUT_LIMIT_CHARS",
    "append",
    "attach_status_to_prompt_vars",
    "content_hash",
    "get_hash",
    "build_long_memory_status",
    "build_long_memory_status_for_cwd",
    "compact_text",
    "read",
    "resolve_long_memory_path",
    "stat",
    "write",
]
