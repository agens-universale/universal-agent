"""Режим наблюдения за правками: git-mode vs cache-mode.

Режим зависит только от ``launch_root`` (snapshot cwd на старте) и доступности
``git``. Nested ``.git`` внутри launch_root не учитывается: смотрим ровно
``launch_root/.git``, иначе вендоренный репозиторий переключал бы режим всему
проекту. Шелл-канал журнала работает только в git-mode.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal, Union

DiffMode = Literal["git-mode", "cache-mode"]


def git_executable_available() -> bool:
    """``git`` есть в PATH и исполняется."""
    return shutil.which("git") is not None


def resolve_mode(launch_root: Union[str, Path]) -> DiffMode:
    """git-mode только если git доступен и есть ``launch_root/.git``; иначе cache-mode."""
    root = Path(launch_root)
    if git_executable_available() and (root / ".git").exists():
        return "git-mode"
    return "cache-mode"
