"""Канонизация путей и tracking-perimeter для Diff-TUI (cache-mode)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional, Sequence, Union

from gc_core.security.path_policy import is_within_any_root, is_within_root, normalize_path_for_policy
from gc_core.security.protected_paths import classify_path, protected_paths

PathLike = Union[str, os.PathLike[str]]


def _os_name() -> str:
    """Имя платформы; отдельная точка подмены для кроссплатформенных тестов."""
    return os.name


def _case_insensitive() -> bool:
    return _os_name() == "nt"


def _normalize_roots(
    launch_root: PathLike,
    extra_paths: Sequence[PathLike] | None,
) -> tuple[Path, list[Path]]:
    root = normalize_path_for_policy(launch_root, launch_root)
    extras: list[Path] = []
    for raw in extra_paths or ():
        extras.append(normalize_path_for_policy(raw, launch_root))
    return root, extras


def in_tracking_perimeter(
    path: PathLike,
    launch_root: PathLike,
    *,
    extra_paths: Sequence[PathLike] | None = None,
) -> bool:
    """Путь внутри ``launch_root`` ∪ ``write_guard.extra_paths`` (всегда, без enabled)."""
    try:
        target = normalize_path_for_policy(path, launch_root)
        root, extras = _normalize_roots(launch_root, extra_paths)
    except (ValueError, FileNotFoundError, OSError):
        return False
    return is_within_any_root(
        target,
        [root, *extras],
        case_insensitive=_case_insensitive(),
    )


def is_protected_from_tracking(
    path: PathLike,
    launch_root: PathLike,
    *,
    protected_extra: Sequence[str] | None = None,
) -> bool:
    """Agent-facing protected: operational / instructions — не трекаем."""
    ps = protected_paths(cwd=str(launch_root), extra=list(protected_extra or ()))
    cls = classify_path(path, ps)
    return cls in ("operational", "instructions")


def is_trackable_path(
    path: PathLike,
    launch_root: PathLike,
    *,
    extra_paths: Sequence[PathLike] | None = None,
    protected_extra: Sequence[str] | None = None,
) -> bool:
    if not in_tracking_perimeter(path, launch_root, extra_paths=extra_paths):
        return False
    return not is_protected_from_tracking(path, launch_root, protected_extra=protected_extra)


def canonical_path_key(
    path: PathLike,
    launch_root: PathLike,
    *,
    extra_paths: Sequence[PathLike] | None = None,
    protected_extra: Sequence[str] | None = None,
) -> Optional[str]:
    """Ключ для ``index.json`` или ``None``, если путь не трекаем.

    Под ``launch_root`` → relative POSIX; под ``extra_paths`` → absolute POSIX.
    """
    try:
        resolved = normalize_path_for_policy(path, launch_root)
        root, extras = _normalize_roots(launch_root, extra_paths)
    except (ValueError, FileNotFoundError, OSError):
        return None

    ci = _case_insensitive()
    if not is_within_any_root(resolved, [root, *extras], case_insensitive=ci):
        return None
    if is_protected_from_tracking(
        resolved, launch_root, protected_extra=protected_extra
    ):
        return None

    if is_within_root(resolved, root, case_insensitive=ci):
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            # case-insensitive match на Windows: relative_to может не сработать
            # при разном регистре — считаем через commonpath.
            rel = os.path.relpath(os.fspath(resolved), os.fspath(root))
            return Path(rel).as_posix()

    return resolved.as_posix()


def orig_key(canonical_key: str) -> str:
    """Имя ``.orig`` без суффикса: sha256 от key_bytes (casefold на Windows)."""
    if _case_insensitive():
        key_bytes = canonical_key.casefold().encode("utf-8")
    else:
        key_bytes = canonical_key.encode("utf-8")
    return hashlib.sha256(key_bytes).hexdigest()
