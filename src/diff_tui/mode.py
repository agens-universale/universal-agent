"""Детектор режима Diff-TUI: git-mode vs cache-mode.

Режим зависит только от ``launch_root`` (snapshot cwd на старте TUI)
и доступности ``git``. Nested ``.git`` внутри launch_root не учитывается.

В git-mode (v2 hybrid) полный wipe ``.gc/diffs/`` отключён — только
точечный stale-cleanup git-visible ключей через ``evict_stale_cache``.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Set, Union

if TYPE_CHECKING:
    from diff_tui.store import DiffStore

DiffMode = Literal["git-mode", "cache-mode"]

_logger = logging.getLogger(__name__)


def git_executable_available() -> bool:
    """``git`` есть в PATH и исполняется."""
    return shutil.which("git") is not None


def resolve_mode(launch_root: Union[str, Path]) -> DiffMode:
    """git-mode только если git доступен и есть ``launch_root/.git``; иначе cache-mode."""
    root = Path(launch_root)
    if git_executable_available() and (root / ".git").exists():
        return "git-mode"
    return "cache-mode"


def evict_stale_cache(
    store: "DiffStore",
    *,
    git_visible_keys: Set[str],
) -> int:
    """Удалить cache-ключи, которые видны git (конфликт → git побеждает).

    Возвращает число удалённых ключей.
    """
    if not git_visible_keys:
        return 0
    removed = 0
    for key in list(store.load_index()):
        if key in git_visible_keys:
            if store.drop_entry(key):
                removed += 1
    if removed:
        _logger.info(
            "evicted %s stale cache key(s) superseded by git", removed
        )
    return removed
