"""Пути к корню репозитория в editable install (recommended_mcp/ вне src/)."""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def repo_root() -> Path | None:
    """Корень monorepo: родитель каталога src/ (где лежит recommended_mcp/)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "src" / "mcp_runtime").is_dir() and (parent / "recommended_mcp").is_dir():
            return parent
    return None


def ensure_repo_on_sys_path() -> Path | None:
    """Добавить src/ и корень репо в sys.path для import recommended_mcp и config_modules."""
    root = repo_root()
    if root is None:
        return None
    src = root / "src"
    for path in (src, root):
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return root
