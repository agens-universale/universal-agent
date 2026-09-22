"""Session-scoped DiffService listing / ops (S1–S12 acceptance)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from gc_core.config_modules import env_vars
from gc_core.runtime_notices import clear_runtime_notices
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
def git_proj(tmp_path, monkeypatch):
    if not _has_git():
        pytest.skip("git недоступен")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    (root / ".gitignore").write_text("local.md\n", encoding="utf-8")
    _git(root, "add", "tracked.txt", ".gitignore")
    _git(root, "commit", "-m", "init")
    clear_runtime_notices()
    threads = tmp_path / "threads"
    threads.mkdir()
    return root, threads


def _svc(root: Path, threads: Path, tid: str, **kw) -> DiffService:
    return DiffService(
        str(root),
        threads_dir=str(threads),
        system_id="tui",
        thread_id=tid,
        emit_notices=False,
        **kw,
    )


def test_s1_journal_path_in_list(git_proj):
    root, threads = git_proj
    (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    TouchedJournal(str(threads), "tui", "A").add("tracked.txt")
    keys = {e.key for e in _svc(root, threads, "A").list_entries(pending_only=True)}
    assert keys == {"tracked.txt"}


def test_s2_foreign_dirty_hidden_without_journal(git_proj):
    root, threads = git_proj
    (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (root / "stray.py").write_text("x\n", encoding="utf-8")
    assert _svc(root, threads, "A").list_entries(pending_only=True) == []


def test_s3_sessions_different_files(git_proj):
    root, threads = git_proj
    (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (root / "other.py").write_text("x\n", encoding="utf-8")
    TouchedJournal(str(threads), "tui", "A").add("tracked.txt")
    TouchedJournal(str(threads), "tui", "B").add("other.py")
    assert {e.key for e in _svc(root, threads, "A").list_entries(pending_only=True)} == {
        "tracked.txt"
    }
    assert {e.key for e in _svc(root, threads, "B").list_entries(pending_only=True)} == {
        "other.py"
    }


def test_s4_shared_file_both_see_same_visual(git_proj):
    root, threads = git_proj
    (root / "tracked.txt").write_text("shared\n", encoding="utf-8")
    TouchedJournal(str(threads), "tui", "A").add("tracked.txt")
    TouchedJournal(str(threads), "tui", "B").add("tracked.txt")
    a = _svc(root, threads, "A")
    b = _svc(root, threads, "B")
    assert {e.key for e in a.list_entries(pending_only=True)} == {"tracked.txt"}
    assert {e.key for e in b.list_entries(pending_only=True)} == {"tracked.txt"}
    assert a.visual_diff("tracked.txt") == b.visual_diff("tracked.txt")


def test_s5_approve_a_only_discards_journal_a(git_proj):
    root, threads = git_proj
    (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    TouchedJournal(str(threads), "tui", "A").add("tracked.txt")
    TouchedJournal(str(threads), "tui", "B").add("tracked.txt")
    assert _svc(root, threads, "A").approve("tracked.txt").ok
    assert "tracked.txt" not in TouchedJournal(str(threads), "tui", "A").paths()
    assert "tracked.txt" in TouchedJournal(str(threads), "tui", "B").paths()


def test_s6_s7_undo_a_lazy_discard_b(git_proj):
    root, threads = git_proj
    (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    TouchedJournal(str(threads), "tui", "A").add("tracked.txt")
    TouchedJournal(str(threads), "tui", "B").add("tracked.txt")
    assert _svc(root, threads, "A").undo("tracked.txt").ok
    assert "tracked.txt" not in TouchedJournal(str(threads), "tui", "A").paths()
    assert "tracked.txt" in TouchedJournal(str(threads), "tui", "B").paths()
    # list B: no delta → hide + lazy discard B
    assert _svc(root, threads, "B").list_entries(pending_only=True) == []
    assert "tracked.txt" not in TouchedJournal(str(threads), "tui", "B").paths()


def test_s8_undo_all_does_not_touch_only_b(git_proj):
    root, threads = git_proj
    (root / "tracked.txt").write_text("a\n", encoding="utf-8")
    (root / "only_b.py").write_text("b\n", encoding="utf-8")
    TouchedJournal(str(threads), "tui", "A").add("tracked.txt")
    TouchedJournal(str(threads), "tui", "B").add("only_b.py")
    result = _svc(root, threads, "A").undo_all()
    assert result.ok
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert (root / "only_b.py").read_text(encoding="utf-8") == "b\n"
    assert "only_b.py" in TouchedJournal(str(threads), "tui", "B").paths()


def test_s9_cache_mode_intersect_touched(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    threads = tmp_path / "threads"
    threads.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    target = root / "src" / "foo.py"
    target.write_text("base\n", encoding="utf-8")
    DiffStore(str(root)).register_mutation(str(target))
    target.write_text("changed\n", encoding="utf-8")
    # без journal — скрыто
    assert _svc(root, threads, "A").list_entries(pending_only=True) == []
    TouchedJournal(str(threads), "tui", "A").add("src/foo.py")
    keys = {e.key for e in _svc(root, threads, "A").list_entries(pending_only=True)}
    assert keys == {"src/foo.py"}


def test_s10_hybrid_ignored_with_journal(git_proj):
    root, threads = git_proj
    target = root / "local.md"
    target.write_text("v1\n", encoding="utf-8")
    DiffStore(str(root)).register_mutation(str(target))
    target.write_text("v2\n", encoding="utf-8")
    TouchedJournal(str(threads), "tui", "A").add("local.md")
    entries = {e.key: e for e in _svc(root, threads, "A").list_entries(pending_only=True)}
    assert entries["local.md"].backend == "cache"


def test_no_identity_empty_list(git_proj):
    root, _threads = git_proj
    (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    svc = DiffService(str(root), emit_notices=False)
    assert svc.list_entries(pending_only=True) == []


def test_lazy_discard_stale_journal_keys(git_proj):
    root, threads = git_proj
    TouchedJournal(str(threads), "tui", "A").add("tracked.txt")
    # clean worktree — no delta
    assert _svc(root, threads, "A").list_entries(pending_only=True) == []
    assert "tracked.txt" not in TouchedJournal(str(threads), "tui", "A").paths()
