"""Детектор режима Diff-TUI: git-mode vs cache-mode.

Режим зависит только от ``launch_root`` (snapshot cwd на старте TUI)
и доступности ``git``. Nested ``.git`` внутри launch_root не учитывается.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Literal, Union

from config_modules.env_vars import project_gc_dir

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


def maybe_clear_cache_on_git(launch_root: Union[str, Path]) -> bool:
    """При входе в git-mode очистить устаревший ``.gc/diffs/`` (cache→git).

    Возвращает True, если что-то удалили.
    """
    if resolve_mode(launch_root) != "git-mode":
        return False
    diffs = project_gc_dir(str(launch_root)) / "diffs"
    if not diffs.exists():
        return False
    removed = False
    for p in diffs.iterdir():
        try:
            if p.is_file():
                p.unlink()
                removed = True
        except OSError:
            pass
    if removed:
        _logger.info("git detected; removed obsolete .gc/diffs cache")
    return removed
