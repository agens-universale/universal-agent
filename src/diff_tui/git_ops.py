"""Тонкие обёртки git для Diff-TUI (cwd=launch_root, timeout). Без UX."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Sequence, Union

PathLike = Union[str, Path]

GIT_TIMEOUT_S = 10

GitStatusKind = Literal["modified", "created", "deleted"]
GitReviewState = Literal["pending", "approved"]


@dataclass(frozen=True)
class GitStatusEntry:
    """Один путь из ``git status --porcelain``."""

    path: str  # relative POSIX from launch_root
    kind: GitStatusKind  # M / A / D для UX
    state: GitReviewState  # pending = worktree≠index или untracked
    xy: str  # сырой двухбуквенный код porcelain


@dataclass(frozen=True)
class GitRunResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def _run_git(launch_root: PathLike, args: Sequence[str]) -> GitRunResult:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(launch_root),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return GitRunResult(ok=False, stderr=str(exc), returncode=-1)
    return GitRunResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        returncode=proc.returncode,
    )


def _kind_from_xy(x: str, y: str) -> GitStatusKind:
    if x == "D" or y == "D":
        return "deleted"
    if x == "A" or x == "?" or y == "?":
        return "created"
    return "modified"


def _parse_porcelain_line(line: str) -> Optional[GitStatusEntry]:
    if len(line) < 4:
        return None
    xy = line[:2]
    rest = line[3:]
    # rename/copy: "R  old -> new" / "C  old -> new" — берём new
    if " -> " in rest:
        rest = rest.split(" -> ", 1)[1]
    path = rest.replace("\\", "/").strip()
    if not path:
        return None

    if xy == "??":
        return GitStatusEntry(
            path=path, kind="created", state="pending", xy=xy
        )

    x, y = xy[0], xy[1]
    # worktree dirty (y != ' ') → pending; чистое staging (x set, y space) → approved
    if y != " ":
        state: GitReviewState = "pending"
    elif x not in (" ", "?"):
        state = "approved"
    else:
        return None

    return GitStatusEntry(
        path=path,
        kind=_kind_from_xy(x, y),
        state=state,
        xy=xy,
    )


def git_status(launch_root: PathLike) -> list[GitStatusEntry]:
    """``git status --porcelain`` → список entries (pending + approved)."""
    result = _run_git(launch_root, ["status", "--porcelain", "-u"])
    if not result.ok and not result.stdout:
        return []
    entries: list[GitStatusEntry] = []
    for line in result.stdout.splitlines():
        parsed = _parse_porcelain_line(line)
        if parsed is not None:
            entries.append(parsed)
    return entries


def git_add(launch_root: PathLike, path: str) -> GitRunResult:
    return _run_git(launch_root, ["add", "--", path])


def git_restore_worktree(launch_root: PathLike, path: str) -> GitRunResult:
    """``git restore --worktree -- <path>`` — worktree ← index; staging не трогаем."""
    return _run_git(launch_root, ["restore", "--worktree", "--", path])


def git_restore_worktree_all(launch_root: PathLike) -> GitRunResult:
    return _run_git(launch_root, ["restore", "--worktree", "--", "."])


def git_diff_worktree(launch_root: PathLike, path: str) -> GitRunResult:
    """Unstaged / pending diff vs index для одного файла."""
    return _run_git(launch_root, ["diff", "--", path])


def check_ignore(launch_root: PathLike, path: str) -> Optional[bool]:
    """``git check-ignore -q -- <path>``.

    Returns:
        ``True`` — путь ignored (exit 0);
        ``False`` — не ignored (exit 1);
        ``None`` — ошибка / git недоступен (fail-safe).
    """
    result = _run_git(launch_root, ["check-ignore", "-q", "--", path])
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None
