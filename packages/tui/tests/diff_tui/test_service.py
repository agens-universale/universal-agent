"""DiffService: cache approve/undo + git semantics + mode switch (T3–T5, T10–T13)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from gc_core.config_modules import env_vars
from gc_core.runtime_notices import clear_runtime_notices
from gc_tui.diff_tui.paths import orig_key
from gc_tui.diff_tui.service import DiffService
from gc_tui.diff_tui.store import DiffStore
from gc_tui.diff_tui.touched import TouchedJournal


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
    threads = tmp_path / "threads"
    threads.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    clear_runtime_notices()
    return root, threads


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    if not _has_git():
        pytest.skip("git недоступен")
    root = tmp_path / "repo"
    root.mkdir()
    threads = tmp_path / "threads"
    threads.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-m", "init")
    clear_runtime_notices()
    return root, threads


def _svc(root, threads, tid="t1", **kw):
    return DiffService(
        str(root),
        threads_dir=str(threads),
        system_id="tui",
        thread_id=tid,
        **kw,
    )


def _touch(threads, *keys, tid="t1"):
    j = TouchedJournal(str(threads), "tui", tid)
    for k in keys:
        j.add(k)


def _orig(store: DiffStore, key: str) -> Path:
    return store.diffs_dir / f"{orig_key(key)}.orig"


# --- cache T3–T5 via DiffService ---


def test_t3_cache_approve_via_service(cache_proj):
    root, threads = cache_proj
    target = root / "src" / "foo.py"
    target.write_text("old\n", encoding="utf-8")
    store = DiffStore(str(root))
    store.register_mutation(str(target))
    target.write_text("approved\n", encoding="utf-8")
    _touch(threads, "src/foo.py")

    svc = _svc(root, threads, emit_notices=True)
    assert svc.mode == "cache-mode"
    assert svc.approve(str(target)).ok
    assert _orig(store, "src/foo.py").read_text(encoding="utf-8") == "approved\n"
    pending = svc.list_entries(pending_only=True)
    assert all(e.key != "src/foo.py" for e in pending)


def test_t4_cache_undo_via_service(cache_proj):
    root, threads = cache_proj
    target = root / "src" / "foo.py"
    target.write_text("base\n", encoding="utf-8")
    DiffStore(str(root)).register_mutation(str(target))
    target.write_text("changed\n", encoding="utf-8")
    _touch(threads, "src/foo.py")

    svc = _svc(root, threads)
    assert svc.undo(str(target)).ok
    assert target.read_text(encoding="utf-8") == "base\n"


def test_t5_cache_undo_created_deletes_file(cache_proj):
    root, threads = cache_proj
    target = root / "src" / "new.py"
    DiffStore(str(root)).register_mutation(str(target))
    target.write_text("brand\n", encoding="utf-8")
    _touch(threads, "src/new.py")

    svc = _svc(root, threads, emit_notices=False)
    result = svc.undo(str(target))
    assert result.ok
    assert result.undone_keys == ("src/new.py",)
    assert result.deleted_keys == ("src/new.py",)
    assert result.restored_keys == ()
    assert not target.exists()
    assert all(e.key != "src/new.py" for e in svc.list_entries())


def test_cache_external_delete_shows_deleted_diff(cache_proj):
    """Файл удалён с диска (rm), .orig жив → kind=deleted и visual_diff не пустой."""
    root, threads = cache_proj
    target = root / "src" / "gone.py"
    target.write_text("keep-me\n", encoding="utf-8")
    DiffStore(str(root)).register_mutation(str(target))
    target.write_text("changed\n", encoding="utf-8")
    target.unlink()
    _touch(threads, "src/gone.py")

    svc = _svc(root, threads, emit_notices=False)
    entries = {e.key: e for e in svc.list_entries(pending_only=True)}
    assert "src/gone.py" in entries
    assert entries["src/gone.py"].kind == "deleted"
    body = svc.visual_diff("src/gone.py")
    assert body.strip()
    assert "/dev/null" in body
    assert "keep-me" in body or "-keep-me" in body or "changed" in body


def test_cache_approve_deleted_clears_pending_and_orig(cache_proj):
    """Approve deleted: pending уходит, .orig и index чистятся (не no-op)."""
    root, threads = cache_proj
    target = root / "src" / "gone.py"
    store = DiffStore(str(root))
    target.write_text("base\n", encoding="utf-8")
    store.register_mutation(str(target))
    target.write_text("changed\n", encoding="utf-8")
    target.unlink()
    _touch(threads, "src/gone.py")

    key = "src/gone.py"
    orig = store.diffs_dir / f"{orig_key(key)}.orig"
    assert orig.is_file()

    svc = _svc(root, threads, emit_notices=False)
    assert svc.approve(key).ok
    assert all(e.key != key for e in svc.list_entries(pending_only=True))
    assert key not in store.load_index()
    assert not orig.exists()


# --- git T11–T12 ---


def test_t11_git_approve_and_undo_keeps_staging(git_repo):
    root, threads = git_repo
    (root / "a.txt").write_text("staged\n", encoding="utf-8")
    _touch(threads, "a.txt")
    svc = _svc(root, threads)
    assert svc.mode == "git-mode"
    assert svc.approve("a.txt").ok

    (root / "a.txt").write_text("dirty\n", encoding="utf-8")
    _touch(threads, "a.txt")
    assert svc.undo("a.txt").ok
    assert (root / "a.txt").read_text(encoding="utf-8") == "staged\n"

    _touch(threads, "a.txt")
    entries = {e.key: e for e in svc.list_entries()}
    assert entries["a.txt"].state == "approved"
    pending = svc.list_entries(pending_only=True)
    assert all(e.key != "a.txt" for e in pending)


def test_t11_git_untracked_undo_deletes_file(git_repo):
    root, threads = git_repo
    (root / "new.txt").write_text("x\n", encoding="utf-8")
    _touch(threads, "new.txt")
    svc = _svc(root, threads, emit_notices=False)
    result = svc.undo("new.txt")
    assert result.ok
    assert result.undone_keys == ("new.txt",)
    assert result.deleted_keys == ("new.txt",)
    assert result.restored_keys == ()
    assert not (root / "new.txt").exists()


def test_git_untracked_visual_diff_shows_file_content(git_repo):
    """``git diff`` пуст для ``??`` — visual_diff показывает содержимое как addition."""
    root, threads = git_repo
    (root / "fresh.py").write_text("hello created\n", encoding="utf-8")
    _touch(threads, "fresh.py")
    svc = _svc(root, threads, emit_notices=False)
    entries = {e.key: e for e in svc.list_entries(pending_only=True)}
    assert entries["fresh.py"].kind == "created"
    body = svc.visual_diff("fresh.py")
    assert body.strip()
    assert "/dev/null" in body
    assert "+++ fresh.py" in body
    assert "+hello created" in body


def test_cache_created_visual_diff_shows_file_content(cache_proj):
    root, threads = cache_proj
    target = root / "src" / "new.py"
    DiffStore(str(root)).register_mutation(str(target))
    target.write_text("brand new\n", encoding="utf-8")

    svc = DiffService(str(root), emit_notices=False)
    body = svc.visual_diff("src/new.py")
    assert body.strip()
    assert "/dev/null" in body
    assert "+brand new" in body


def test_cache_created_empty_file_visual_diff_not_blank(cache_proj):
    root, threads = cache_proj
    target = root / "src" / "empty.py"
    DiffStore(str(root)).register_mutation(str(target))
    target.write_text("", encoding="utf-8")

    svc = DiffService(str(root), emit_notices=False)
    body = svc.visual_diff("src/empty.py")
    assert body.strip()
    assert "/dev/null" in body
    assert "+++ src/empty.py" in body


def test_t12_git_deleted_unstaged_undo_restores(git_repo):
    root, threads = git_repo
    (root / "a.txt").unlink()
    _touch(threads, "a.txt")
    svc = _svc(root, threads)
    entries = svc.list_entries(pending_only=True)
    assert entries[0].kind == "deleted"
    assert svc.undo("a.txt").ok
    assert (root / "a.txt").read_text(encoding="utf-8") == "base\n"


def test_t12_git_deleted_staged_undo_keeps_deleted(git_repo):
    root, threads = git_repo
    (root / "a.txt").unlink()
    _touch(threads, "a.txt")
    svc = _svc(root, threads)
    assert svc.approve("a.txt").ok
    assert not (root / "a.txt").exists()
    _touch(threads, "a.txt")
    assert svc.undo("a.txt").ok
    assert not (root / "a.txt").exists()


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
    target = root / "src" / "foo.py"
    target.write_text("x\n", encoding="utf-8")
    store = DiffStore(str(root))
    store.register_mutation(str(target))
    assert store.index_path.is_file()
    assert list(store.diffs_dir.glob("*.orig"))

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



# --- spec §5 step 3: git_show_index + visual_diff baseline↔disk ---


def test_git_show_index_returns_staged_content(git_repo):
    from gc_tui.diff_tui import git_ops

    root, _threads = git_repo
    # a.txt staged with "base\n"
    res = git_ops.git_show_index(str(root), "a.txt")
    assert res.ok
    assert res.stdout == "base\n"


def test_git_show_index_untracked_fails(git_repo):
    from gc_tui.diff_tui import git_ops

    root, _threads = git_repo
    (root / "fresh.py").write_text("x\n", encoding="utf-8")
    res = git_ops.git_show_index(str(root), "fresh.py")
    assert not res.ok


def test_git_modified_visual_diff_uses_index_baseline(git_repo):
    """Modified unstaged: visual_diff = index↔disk через difflib, не сырой git diff."""
    root, threads = git_repo
    (root / "a.txt").write_text("base\nCHANGED\n", encoding="utf-8")
    _touch(threads, "a.txt")
    svc = _svc(root, threads, emit_notices=False)
    entries = {e.key: e for e in svc.list_entries(pending_only=True)}
    assert entries["a.txt"].kind == "modified"
    body = svc.visual_diff("a.txt")
    assert body.strip()
    assert "+++ b/a.txt" in body or "+++ a.txt" in body
    assert "-base" in body or "base" in body
    assert "+CHANGED" in body


def test_git_modified_visual_diff_short_shows_whole_file(git_repo):
    """Adaptive: короткий modified → весь файл в контексте (без @@ разрывов)."""
    root, threads = git_repo
    content = "".join(f"line-{i}\n" for i in range(10))
    (root / "a.txt").write_text(content, encoding="utf-8")
    _git(root, "add", "a.txt")
    (root / "a.txt").write_text(content + "EXTRA\n", encoding="utf-8")
    _touch(threads, "a.txt")
    svc = _svc(root, threads, emit_notices=False, full_file_max_lines=300,
               preview_context_lines=50)
    body = svc.visual_diff("a.txt")
    # вся база видна: line-9 в контексте
    assert "line-9" in body
    assert "+EXTRA" in body


def test_git_modified_visual_diff_long_uses_preview_context(git_repo):
    """Adaptive: длинный modified → preview_context_lines, far строки не видны."""
    root, threads = git_repo
    base_lines = [f"line-{i}\n" for i in range(400)]
    (root / "a.txt").write_text("".join(base_lines), encoding="utf-8")
    _git(root, "add", "a.txt")
    new_lines = ["line-0\n", "CHANGED\n"] + base_lines[2:]
    (root / "a.txt").write_text("".join(new_lines), encoding="utf-8")
    _touch(threads, "a.txt")
    svc = _svc(root, threads, emit_notices=False, full_file_max_lines=300,
               preview_context_lines=2)
    body = svc.visual_diff("a.txt")
    assert "+CHANGED" in body
    assert "line-399" not in body  # far от изменения


def test_visual_diff_accepts_mode_full(git_repo):
    """visual_diff(path, mode='full') → весь файл даже при >300 строк."""
    root, threads = git_repo
    base_lines = [f"line-{i}\n" for i in range(400)]
    (root / "a.txt").write_text("".join(base_lines), encoding="utf-8")
    _git(root, "add", "a.txt")
    new_lines = ["line-0\n", "CHANGED\n"] + base_lines[2:]
    (root / "a.txt").write_text("".join(new_lines), encoding="utf-8")
    _touch(threads, "a.txt")
    svc = _svc(root, threads, emit_notices=False, full_file_max_lines=300,
               preview_context_lines=50)
    body = svc.visual_diff("a.txt", mode="full")
    assert "line-399" in body
    assert "+CHANGED" in body


def test_visual_diff_invalid_mode_raises(git_repo):
    root, threads = git_repo
    (root / "a.txt").write_text("base\nCHANGED\n", encoding="utf-8")
    _touch(threads, "a.txt")
    svc = _svc(root, threads, emit_notices=False)
    with pytest.raises(ValueError):
        svc.visual_diff("a.txt", mode="side")


def test_visual_diff_truncates_max_diff_chars(git_repo):
    root, threads = git_repo
    (root / "a.txt").write_text("base\n", encoding="utf-8")
    big = "".join(f"x-{i}\n" for i in range(2000))
    (root / "a.txt").write_text(big, encoding="utf-8")
    _touch(threads, "a.txt")
    svc = _svc(root, threads, emit_notices=False, max_diff_chars=50)
    body = svc.visual_diff("a.txt")
    assert "[diff truncated]" in body
