"""Путь к корню репозитория в editable-установке.

`recommended_mcp/` — отдельный импортируемый пакет и лежит в корне репозитория,
а не внутри `packages/*/src`. Чтобы `import recommended_mcp` работал из
dev-окружения, корень нужно найти и положить на `sys.path`.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def repo_root() -> Path | None:
    """Корень репозитория: каталог, где лежат `packages/` и `recommended_mcp/`.

    Маркер — пара «каталог пакетов + recommended_mcp». До разбиения на модули
    искали `src/mcp_runtime`; такого пути больше нет, поэтому проверка идёт по
    раскладке репозитория, а не по конкретному модулю. Ветка с `src/`
    оставлена для дореформенных рабочих копий.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if not (parent / "recommended_mcp").is_dir():
            continue
        if (parent / "packages").is_dir() or (parent / "src" / "mcp_runtime").is_dir():
            return parent
    return None


def ensure_repo_on_sys_path() -> Path | None:
    """Положить на `sys.path` корень репо и каталоги исходников всех пакетов."""
    root = repo_root()
    if root is None:
        return None
    candidates = [root, *sorted(root.glob("packages/*/src"))]
    legacy_src = root / "src"
    if legacy_src.is_dir():
        candidates.append(legacy_src)
    for path in candidates:
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return root
