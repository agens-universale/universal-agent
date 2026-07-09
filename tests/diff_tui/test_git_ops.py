"""git_ops: status / add / restore --worktree (tmp git repo)."""
from __future__ import annotations

import subprocess

import pytest

from diff_tui import git_ops


def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path):
    if not _has_git():
        pytest.skip("git недоступен")
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "tracked.txt").write_text("v1\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_status_modified_pending(repo):
    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    entries = git_ops.git_status(repo)
    assert len(entries) == 1
    e = entries[0]
    assert e.path == "tracked.txt"
    assert e.kind == "modified"
    assert e.state == "pending"


def test_status_untracked_pending(repo):
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    entries = {e.path: e for e in git_ops.git_status(repo)}
    assert entries["new.txt"].kind == "created"
    assert entries["new.txt"].state == "pending"
    assert entries["new.txt"].xy == "??"


def test_approve_stages_then_approved(repo):
    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    assert git_ops.git_add(repo, "tracked.txt").ok
    entries = git_ops.git_status(repo)
    assert len(entries) == 1
    assert entries[0].state == "approved"
    assert entries[0].kind == "modified"


def test_undo_restores_worktree_keeps_staging(repo):
    (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
    assert git_ops.git_add(repo, "tracked.txt").ok
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    assert git_ops.git_restore_worktree(repo, "tracked.txt").ok
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "staged\n"
    # staging сохранён: status approved (M в index, worktree clean)
    entries = git_ops.git_status(repo)
    assert len(entries) == 1
    assert entries[0].state == "approved"


def test_deleted_pending_and_undo_restores(repo):
    (repo / "tracked.txt").unlink()
    entries = git_ops.git_status(repo)
    assert entries[0].kind == "deleted"
    assert entries[0].state == "pending"
    assert git_ops.git_restore_worktree(repo, "tracked.txt").ok
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "v1\n"


def test_deleted_staged_status_approved(repo):
    """После ``git add`` удаления: status=approved deleted; worktree без файла."""
    (repo / "tracked.txt").unlink()
    assert git_ops.git_add(repo, "tracked.txt").ok
    entries = git_ops.git_status(repo)
    assert entries[0].kind == "deleted"
    assert entries[0].state == "approved"
    assert not (repo / "tracked.txt").exists()
    # raw restore на staged-delete pathspec падает — семантика в DiffService
    assert not git_ops.git_restore_worktree(repo, "tracked.txt").ok
    assert not (repo / "tracked.txt").exists()
