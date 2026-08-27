"""Путь к корню репозитория в editable-установке.

Рецепты (`recommended_mcp`) — отдельный дистрибутив, и в рабочей копии их
исходники лежат в `packages/mcp/src/`. Чтобы `import recommended_mcp` работал
из dev-окружения, корень репозитория нужно найти и положить на `sys.path`
вместе с каталогами исходников всех пакетов.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def repo_root() -> Path | None:
    """Корень репозитория, или None если код запущен не из рабочей копии.

    Маркер — пара «каталог packages/ + pyproject.toml». Раньше маркером был
    `recommended_mcp/` в корне, но рецепты уехали в packages/mcp, и та проверка
    молча перестала бы находить корень: repo_root() вернул бы None, а вместе с
    ним отвалились бы recipe_root(), автозапуск daemon'ов и бутстрап тестов.
    Признак «есть packages/ рядом с pyproject.toml» переживает такие переезды.

    Тот же маркер использует gc_core.bundled_assets.repo_root(); здесь
    отдельная функция, потому что она кэширована и умеет legacy-раскладку.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "packages").is_dir() and (parent / "pyproject.toml").is_file():
            return parent
        # Дореформенные рабочие копии: до разбиения на пакеты был единый src/.
        if (parent / "src" / "mcp_runtime").is_dir():
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
