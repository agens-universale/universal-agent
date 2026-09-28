"""resolve_mode: git-mode только при git + launch_root/.git; nested не переключает."""

from __future__ import annotations

import subprocess

import pytest

from gc_core.agent_edits.mode import resolve_mode


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def test_no_git_dir_is_cache_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gc_core.agent_edits.mode.git_executable_available", lambda: True
    )
    assert resolve_mode(tmp_path) == "cache-mode"


def test_git_unavailable_is_cache_mode_even_with_dot_git(
    tmp_path, monkeypatch
):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        "gc_core.agent_edits.mode.git_executable_available", lambda: False
    )
    assert resolve_mode(tmp_path) == "cache-mode"


def test_launch_root_dot_git_is_git_mode(tmp_path, monkeypatch):
    if not _has_git():
        pytest.skip("git недоступен")
    monkeypatch.setattr(
        "gc_core.agent_edits.mode.git_executable_available", lambda: True
    )
    _git(tmp_path, "init", "-b", "main")
    assert resolve_mode(tmp_path) == "git-mode"


def test_nested_dot_git_does_not_switch_mode(tmp_path, monkeypatch):
    """Nested repo внутри launch_root не переключает режим — смотрим только launch_root/.git."""
    monkeypatch.setattr(
        "gc_core.agent_edits.mode.git_executable_available", lambda: True
    )
    nested = tmp_path / "vendor" / "lib"
    nested.mkdir(parents=True)
    if _has_git():
        _git(nested, "init", "-b", "nested")
    else:
        (nested / ".git").mkdir()
    assert (nested / ".git").exists()
    assert not (tmp_path / ".git").exists()
    assert resolve_mode(tmp_path) == "cache-mode"


def test_resolve_mode_accepts_str_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gc_core.agent_edits.mode.git_executable_available", lambda: True
    )
    assert resolve_mode(str(tmp_path)) == "cache-mode"
