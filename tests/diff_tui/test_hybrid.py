"""Diff-TUI v2 hybrid: ignored→cache в git-mode (H1–H12)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from config_modules import env_vars
from diff_tui.feedback import (
    format_approve_ack,
    format_bulk_approve_ack,
    format_bulk_undo_ack,
)
from diff_tui.mode import evict_stale_cache
from diff_tui.paths import orig_key
from diff_tui.service import DiffService
from diff_tui.store import DiffStore
from runtime_notices import clear_runtime_notices
from tests.helpers.tool_ctx import local_ctx
from tools.fs.write_to_file_tool import WriteToFile


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
    (root / ".gitignore").write_text("local.md\nignored/\n", encoding="utf-8")
    _git(root, "add", "tracked.txt", ".gitignore")
    _git(root, "commit", "-m", "init")
    clear_runtime_notices()
    return root


def _orig(store: DiffStore, key: str) -> Path:
    return store.diffs_dir / f"{orig_key(key)}.orig"


def _cfg(launch_root: Path) -> dict:
    return {
        "launch_root": str(launch_root),
        "write_guard": {"enabled": False, "mode": "deny", "extra_paths": []},
        "diff_tui": {
            "cache_max_orig": 500,
            "preview_context_lines": 3,
            "max_diff_chars": 8000,
        },
    }


def _write_tool(launch_root: Path) -> WriteToFile:
    ctx = local_ctx(_cfg(launch_root))
    ctx.is_tui = True
    return WriteToFile(ctx=ctx)


# --- backend_for ---


def test_backend_for_ignored_is_cache(git_proj):
    (git_proj / "local.md").write_text("x\n", encoding="utf-8")
    svc = DiffService(str(git_proj), emit_notices=False)
    assert svc.backend_for("local.md") == "cache"


def test_backend_for_tracked_is_git(git_proj):
    svc = DiffService(str(git_proj), emit_notices=False)
    assert svc.backend_for("tracked.txt") == "git"


def test_backend_for_untracked_non_ignored_is_git(git_proj):
    (git_proj / "new.txt").write_text("x\n", encoding="utf-8")
    svc = DiffService(str(git_proj), emit_notices=False)
    assert svc.backend_for("new.txt") == "git"


def test_backend_for_outside_perimeter_is_none(git_proj, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n", encoding="utf-8")
    svc = DiffService(str(git_proj), emit_notices=False)
    assert svc.backend_for(str(outside)) is None


def test_backend_for_cache_mode_is_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("x\n", encoding="utf-8")
    svc = DiffService(str(root), emit_notices=False)
    assert svc.mode == "cache-mode"
    assert svc.backend_for("a.txt") == "cache"


# --- H1–H4 ignored via tracking + service ---


def test_h1_ignored_mutation_pending_cache_and_orig(git_proj):
    target = git_proj / "local.md"
    target.write_text("v1\n", encoding="utf-8")
    tool = _write_tool(git_proj)
    assert json.loads(tool(path=str(target), content="v2\n"))["ok"] is True

    store = DiffStore(str(git_proj))
    assert _orig(store, "local.md").read_text(encoding="utf-8") == "v1\n"
    index = store.load_index()
    assert "local.md" in index

    svc = DiffService(str(git_proj), emit_notices=False)
    entries = {e.key: e for e in svc.list_entries(pending_only=True)}
    assert "local.md" in entries
    assert entries["local.md"].backend == "cache"


def test_h2_visual_diff_ignored_vs_orig(git_proj):
    target = git_proj / "local.md"
    target.write_text("v1\n", encoding="utf-8")
    tool = _write_tool(git_proj)
    assert json.loads(tool(path=str(target), content="v2\n"))["ok"] is True

    svc = DiffService(str(git_proj), emit_notices=False)
    body = svc.visual_diff("local.md")
    assert "v1" in body
    assert "v2" in body
    assert "local.md" in body


def test_h3_approve_ignored_updates_orig_not_staged(git_proj):
    target = git_proj / "local.md"
    target.write_text("v1\n", encoding="utf-8")
    tool = _write_tool(git_proj)
    assert json.loads(tool(path=str(target), content="v2\n"))["ok"] is True

    svc = DiffService(str(git_proj), emit_notices=False)
    result = svc.approve("local.md")
    assert result.ok
    assert result.backend == "cache"
    store = DiffStore(str(git_proj))
    assert _orig(store, "local.md").read_text(encoding="utf-8") == "v2\n"
    # не в git status (ignored)
    from diff_tui import git_ops

    assert all(e.path != "local.md" for e in git_ops.git_status(git_proj))
    ack = format_approve_ack(1, "local.md")
    assert ack is not None
    assert "staged" not in ack.lower()


def test_h4_undo_ignored_restores_orig(git_proj):
    target = git_proj / "local.md"
    target.write_text("v1\n", encoding="utf-8")
    tool = _write_tool(git_proj)
    assert json.loads(tool(path=str(target), content="v2\n"))["ok"] is True

    svc = DiffService(str(git_proj), emit_notices=False)
    assert svc.undo("local.md").ok
    assert target.read_text(encoding="utf-8") == "v1\n"


# --- H5–H6 tracked / ?? stay on git ---


def test_h5_tracked_mutation_uses_git_not_cache(git_proj):
    target = git_proj / "tracked.txt"
    tool = _write_tool(git_proj)
    assert json.loads(tool(path=str(target), content="changed\n"))["ok"] is True
    assert DiffStore(str(git_proj)).load_index() == {}

    svc = DiffService(str(git_proj), emit_notices=False)
    assert svc.backend_for("tracked.txt") == "git"
    entries = {e.key: e for e in svc.list_entries(pending_only=True)}
    assert entries["tracked.txt"].backend == "git"


def test_h6_untracked_non_ignored_uses_git(git_proj):
    target = git_proj / "fresh.py"
    tool = _write_tool(git_proj)
    assert json.loads(tool(path=str(target), content="x\n"))["ok"] is True
    assert DiffStore(str(git_proj)).load_index() == {}

    svc = DiffService(str(git_proj), emit_notices=False)
    assert svc.backend_for("fresh.py") == "git"
    entries = {e.key: e for e in svc.list_entries(pending_only=True)}
    assert entries["fresh.py"].backend == "git"
    assert entries["fresh.py"].kind == "created"


# --- H7 merge list + marker + counter ---


def test_h7_merge_list_marker_and_count(git_proj):
    (git_proj / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    target = git_proj / "local.md"
    target.write_text("v1\n", encoding="utf-8")
    tool = _write_tool(git_proj)
    assert json.loads(tool(path=str(target), content="v2\n"))["ok"] is True

    svc = DiffService(str(git_proj), emit_notices=False)
    entries = svc.list_entries(pending_only=True)
    by_key = {e.key: e for e in entries}
    assert "tracked.txt" in by_key and by_key["tracked.txt"].backend == "git"
    assert "local.md" in by_key and by_key["local.md"].backend == "cache"
    assert len(entries) == 2


# --- H8 stale cleanup when no longer ignored ---


def test_h8_no_longer_ignored_stale_cleanup_to_git(git_proj):
    target = git_proj / "local.md"
    target.write_text("v1\n", encoding="utf-8")
    tool = _write_tool(git_proj)
    assert json.loads(tool(path=str(target), content="v2\n"))["ok"] is True
    store = DiffStore(str(git_proj))
    assert "local.md" in store.load_index()

    # перестал быть ignored → git-visible untracked
    (git_proj / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    svc = DiffService(str(git_proj), emit_notices=False)
    entries = {e.key: e for e in svc.list_entries(pending_only=True)}
    assert "local.md" in entries
    assert entries["local.md"].backend == "git"
    assert "local.md" not in store.load_index()


# --- H9 all-ops both layers ---


def test_h9_approve_all_both_layers_dual_notice(git_proj):
    (git_proj / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    target = git_proj / "local.md"
    target.write_text("v1\n", encoding="utf-8")
    tool = _write_tool(git_proj)
    assert json.loads(tool(path=str(target), content="v2\n"))["ok"] is True

    svc = DiffService(str(git_proj), emit_notices=False)
    result = svc.approve_all()
    assert result.ok
    assert result.git_count == 1
    assert result.cache_count == 1
    ack = format_bulk_approve_ack(result.git_count, result.cache_count)
    assert ack is not None
    assert "git: 1" in ack
    assert "cache: 1" in ack
    assert svc.list_entries(pending_only=True) == []


def test_h9_undo_all_both_layers(git_proj):
    (git_proj / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    target = git_proj / "local.md"
    target.write_text("v1\n", encoding="utf-8")
    tool = _write_tool(git_proj)
    assert json.loads(tool(path=str(target), content="v2\n"))["ok"] is True

    svc = DiffService(str(git_proj), emit_notices=False)
    result = svc.undo_all()
    assert result.ok
    assert result.git_count >= 1
    assert result.cache_count >= 1
    assert (git_proj / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert target.read_text(encoding="utf-8") == "v1\n"
    ack = format_bulk_undo_ack(result.git_count, result.cache_count)
    assert ack is not None
    assert "git" in ack and "cache" in ack


# --- H10 no wipe on git-mode entry ---


def test_h10_git_mode_entry_keeps_ignored_orig(tmp_path, monkeypatch):
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
    assert list(store.diffs_dir.glob("*.orig"))

    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text("src/\n", encoding="utf-8")

    # полный wipe отключён: ignored .orig сохраняется
    svc = DiffService(str(root), emit_notices=False)
    assert svc.mode == "git-mode"
    svc.ensure_mode_ready()
    assert store.index_path.exists()
    assert list(store.diffs_dir.glob("*.orig"))


# --- H11 check-ignore fail / cache-mode ---


def test_h11_check_ignore_fail_no_blind_cache(git_proj, monkeypatch):
    from diff_tui import git_ops

    monkeypatch.setattr(git_ops, "check_ignore", lambda *_a, **_k: None)
    svc = DiffService(str(git_proj), emit_notices=False)
    # путь не в status → None (не трекаем вслепую)
    assert svc.backend_for("ghost.txt") is None
    # путь в status → git
    (git_proj / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    assert svc.backend_for("tracked.txt") == "git"


def test_h11_cache_mode_without_git_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    store = DiffStore(str(root))
    store.register_mutation(str(root / "a.txt"))
    (root / "a.txt").write_text("v2\n", encoding="utf-8")
    svc = DiffService(str(root), emit_notices=False)
    assert svc.mode == "cache-mode"
    assert len(svc.list_entries(pending_only=True)) == 1
    assert svc.approve("a.txt").ok


# --- H12 clear_all in git-mode only cache ---


def test_h12_clear_all_git_mode_only_cache(git_proj):
    (git_proj / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    target = git_proj / "local.md"
    target.write_text("v1\n", encoding="utf-8")
    tool = _write_tool(git_proj)
    assert json.loads(tool(path=str(target), content="v2\n"))["ok"] is True

    svc = DiffService(str(git_proj), emit_notices=False)
    assert svc.clear_all().ok
    store = DiffStore(str(git_proj))
    assert store.load_index() == {}
    assert list(store.diffs_dir.glob("*.orig")) == []
    # git pending остаётся
    pending = svc.list_entries(pending_only=True)
    assert any(e.key == "tracked.txt" for e in pending)
    assert all(e.backend == "git" for e in pending)


def test_evict_stale_cache_removes_git_visible(git_proj):
    store = DiffStore(str(git_proj))
    # симулируем stale cache на tracked path
    target = git_proj / "tracked.txt"
    store.register_mutation(str(target))
    assert "tracked.txt" in store.load_index()
    n = evict_stale_cache(store, git_visible_keys={"tracked.txt"})
    assert n == 1
    assert "tracked.txt" not in store.load_index()
