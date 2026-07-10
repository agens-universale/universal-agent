"""DiffService: cache approve/undo + git semantics + mode switch (T3–T5, T10–T13)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from config_modules import env_vars
from diff_tui.paths import orig_key
from diff_tui.service import DiffService
from diff_tui.store import DiffStore
from runtime_notices import clear_runtime_notices, collect_runtime_notices


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
def cache_proj(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    clear_runtime_notices()
    return root


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    if not _has_git():
        pytest.skip("git недоступен")
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-m", "init")
    clear_runtime_notices()
    return root


def _orig(store: DiffStore, key: str) -> Path:
    return store.diffs_dir / f"{orig_key(key)}.orig"


# --- cache T3–T5 via DiffService ---


def test_t3_cache_approve_via_service(cache_proj):
    target = cache_proj / "src" / "foo.py"
    target.write_text("old\n", encoding="utf-8")
    store = DiffStore(str(cache_proj))
    store.register_mutation(str(target))
    target.write_text("approved\n", encoding="utf-8")

    svc = DiffService(str(cache_proj), emit_notices=True)
    assert svc.mode == "cache-mode"
    assert svc.approve(str(target)).ok
    assert _orig(store, "src/foo.py").read_text(encoding="utf-8") == "approved\n"
    # disk == baseline → не pending
    pending = svc.list_entries(pending_only=True)
    assert all(e.key != "src/foo.py" for e in pending)


def test_t4_cache_undo_via_service(cache_proj):
    target = cache_proj / "src" / "foo.py"
    target.write_text("base\n", encoding="utf-8")
    DiffStore(str(cache_proj)).register_mutation(str(target))
    target.write_text("changed\n", encoding="utf-8")

    svc = DiffService(str(cache_proj))
    assert svc.undo(str(target)).ok
    assert target.read_text(encoding="utf-8") == "base\n"


def test_t5_cache_undo_created_noop_notice(cache_proj):
    target = cache_proj / "src" / "new.py"
    DiffStore(str(cache_proj)).register_mutation(str(target))
    target.write_text("brand\n", encoding="utf-8")

    svc = DiffService(str(cache_proj), emit_notices=True)
    result = svc.undo(str(target))
    assert result.ok is False
    assert result.reason == "created_no_approve"
    assert target.read_text(encoding="utf-8") == "brand\n"
    notices = collect_runtime_notices()
    assert any(n.code == "diff_tui_undo_created" for n in notices)


def test_cache_external_delete_shows_deleted_diff(cache_proj):
    """Файл удалён с диска (rm), .orig жив → kind=deleted и visual_diff не пустой."""
    target = cache_proj / "src" / "gone.py"
    target.write_text("keep-me\n", encoding="utf-8")
    DiffStore(str(cache_proj)).register_mutation(str(target))
    target.write_text("changed\n", encoding="utf-8")
    target.unlink()

    svc = DiffService(str(cache_proj), emit_notices=False)
    entries = {e.key: e for e in svc.list_entries(pending_only=True)}
    assert "src/gone.py" in entries
    assert entries["src/gone.py"].kind == "deleted"
    body = svc.visual_diff("src/gone.py")
    assert body.strip()
    assert "/dev/null" in body
    assert "keep-me" in body or "-keep-me" in body or "changed" in body


# --- git T11–T12 ---


def test_t11_git_approve_and_undo_keeps_staging(git_repo):
    (git_repo / "a.txt").write_text("staged\n", encoding="utf-8")
    svc = DiffService(str(git_repo))
    assert svc.mode == "git-mode"
    assert svc.approve("a.txt").ok

    (git_repo / "a.txt").write_text("dirty\n", encoding="utf-8")
    assert svc.undo("a.txt").ok
    assert (git_repo / "a.txt").read_text(encoding="utf-8") == "staged\n"

    entries = {e.key: e for e in svc.list_entries()}
    assert entries["a.txt"].state == "approved"
    # staging ≠ HEAD: файл всё ещё в списке как approved
    pending = svc.list_entries(pending_only=True)
    assert all(e.key != "a.txt" for e in pending)


def test_t11_git_untracked_undo_noop_notice(git_repo):
    (git_repo / "new.txt").write_text("x\n", encoding="utf-8")
    svc = DiffService(str(git_repo), emit_notices=True)
    result = svc.undo("new.txt")
    assert result.ok is False
    assert result.reason == "untracked"
    assert (git_repo / "new.txt").exists()
    notices = collect_runtime_notices()
    assert any(n.code == "diff_tui_undo_untracked" for n in notices)


def test_t12_git_deleted_unstaged_undo_restores(git_repo):
    (git_repo / "a.txt").unlink()
    svc = DiffService(str(git_repo))
    entries = svc.list_entries(pending_only=True)
    assert entries[0].kind == "deleted"
    assert svc.undo("a.txt").ok
    assert (git_repo / "a.txt").read_text(encoding="utf-8") == "base\n"


def test_t12_git_deleted_staged_undo_keeps_deleted(git_repo):
    (git_repo / "a.txt").unlink()
    svc = DiffService(str(git_repo))
    assert svc.approve("a.txt").ok
    assert not (git_repo / "a.txt").exists()
    # undo после staged delete → worktree←index → файл не появляется
    assert svc.undo("a.txt").ok
    assert not (git_repo / "a.txt").exists()


# --- T10 / T13 mode + cache clear ---


def test_t10_nested_git_does_not_switch_service_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    root = tmp_path / "proj"
    root.mkdir()
    nested = root / "vendor"
    nested.mkdir()
    if _has_git():
        _git(nested, "init", "-b", "nested")
    else:
        (nested / ".git").mkdir()
    svc = DiffService(str(root))
    assert svc.mode == "cache-mode"


def test_t13_cache_to_git_keeps_ignored_cache(tmp_path, monkeypatch):
    """v2: вход в git-mode не wipe'ает ``.gc/diffs/`` (только stale eviction)."""
    if not _has_git():
        pytest.skip("git недоступен")
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    # накопить cache
    target = root / "src" / "foo.py"
    target.write_text("x\n", encoding="utf-8")
    store = DiffStore(str(root))
    store.register_mutation(str(target))
    assert store.index_path.is_file()
    assert list(store.diffs_dir.glob("*.orig"))

    # появился .git → git-mode, но ignored cache сохраняется
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text("src/\n", encoding="utf-8")
    svc = DiffService(str(root))
    assert svc.mode == "git-mode"
    svc.ensure_mode_ready()
    assert store.index_path.exists()
    assert list(store.diffs_dir.glob("*.orig"))
    assert "src/foo.py" in store.load_index()
