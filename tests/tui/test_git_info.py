"""current_branch — git-ветка cwd; не-git/detached/нет git → None."""
import subprocess

import pytest

from gc_tui.services.git_info import current_branch


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def test_non_git_dir_returns_none(tmp_path):
    assert current_branch(str(tmp_path)) is None


def test_git_repo_returns_branch(tmp_path):
    if not _has_git():
        pytest.skip("git недоступен")
    _git(tmp_path, "init", "-b", "feature-x")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-m", "init")
    assert current_branch(str(tmp_path)) == "feature-x"
