"""DiffService: cache approve/undo + git semantics + mode switch (T3–T5, T10–T13)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from config_modules import env_vars
from diff_tui.paths import orig_key
from diff_tui.service import DiffService
from diff_tui.store import DiffStore
from runtime_notices import clear_runtime_notices


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


def test_t5_cache_undo_created_deletes_file(cache_proj):
    target = cache_proj / "src" / "new.py"
    DiffStore(str(cache_proj)).register_mutation(str(target))
    target.write_text("brand\n", encoding="utf-8")

    svc = DiffService(str(cache_proj), emit_notices=False)
    result = svc.undo(str(target))
    assert result.ok
    assert result.undone_keys == ("src/new.py",)
    assert result.deleted_keys == ("src/new.py",)
    assert result.restored_keys == ()
    assert not target.exists()
    assert all(e.key != "src/new.py" for e in svc.list_entries())


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


def test_cache_approve_deleted_clears_pending_and_orig(cache_proj):
    """Approve deleted: pending уходит, .orig и index чистятся (не no-op)."""
    target = cache_proj / "src" / "gone.py"
    store = DiffStore(str(cache_proj))
    target.write_text("base\n", encoding="utf-8")
    store.register_mutation(str(target))
    target.write_text("changed\n", encoding="utf-8")
    target.unlink()

    key = "src/gone.py"
    orig = store.diffs_dir / f"{orig_key(key)}.orig"
    assert orig.is_file()

    svc = DiffService(str(cache_proj), emit_notices=False)
    assert svc.approve(key).ok
    assert all(e.key != key for e in svc.list_entries(pending_only=True))
    assert key not in store.load_index()
    assert not orig.exists()


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


def test_t11_git_untracked_undo_deletes_file(git_repo):
    (git_repo / "new.txt").write_text("x\n", encoding="utf-8")
    svc = DiffService(str(git_repo), emit_notices=False)
    result = svc.undo("new.txt")
    assert result.ok
    assert result.undone_keys == ("new.txt",)
    assert result.deleted_keys == ("new.txt",)
    assert result.restored_keys == ()
    assert not (git_repo / "new.txt").exists()


def test_git_untracked_visual_diff_shows_file_content(git_repo):
    """``git diff`` пуст для ``??`` — visual_diff показывает содержимое как addition."""
    (git_repo / "fresh.py").write_text("hello created\n", encoding="utf-8")
    svc = DiffService(str(git_repo), emit_notices=False)
    entries = {e.key: e for e in svc.list_entries(pending_only=True)}
    assert entries["fresh.py"].kind == "created"
    body = svc.visual_diff("fresh.py")
    assert body.strip()
    assert "/dev/null" in body
    assert "+++ fresh.py" in body
    assert "+hello created" in body


def test_cache_created_visual_diff_shows_file_content(cache_proj):
    target = cache_proj / "src" / "new.py"
    DiffStore(str(cache_proj)).register_mutation(str(target))
    target.write_text("brand new\n", encoding="utf-8")

    svc = DiffService(str(cache_proj), emit_notices=False)
    body = svc.visual_diff("src/new.py")
    assert body.strip()
    assert "/dev/null" in body
    assert "+brand new" in body


def test_cache_created_empty_file_visual_diff_not_blank(cache_proj):
    target = cache_proj / "src" / "empty.py"
    DiffStore(str(cache_proj)).register_mutation(str(target))
    target.write_text("", encoding="utf-8")

    svc = DiffService(str(cache_proj), emit_notices=False)
    body = svc.visual_diff("src/empty.py")
    assert body.strip()
    assert "/dev/null" in body
    assert "+++ src/empty.py" in body


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
