"""Tracking hook: snapshot через реальные FS text-тулы (spec2 T1–T2, T7).

Только TUI (``ctx.is_tui=True``); CLI/server — no-op.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from config_modules import env_vars
from diff_tui.paths import orig_key
from diff_tui.store import DiffStore
from tools.backends.local import LocalFsBackend
from tools.fs.append_to_file import AppendToFile
from tools.fs.apply_file_edits import ApplyFileEdits
from tools.fs.search_and_replace import SearchAndReplace
from tools.fs.write_to_file_tool import WriteToFile


def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.fixture
def launch_root(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    monkeypatch.chdir(root)
    return root


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


def _tool_ctx(launch_root: Path, *, is_tui: bool = True) -> SimpleNamespace:
    cfg = _cfg(launch_root)
    return SimpleNamespace(
        config_dict=cfg,
        os_backend=LocalFsBackend(),
        is_tui=is_tui,
    )


def _write_tool(launch_root: Path, *, is_tui: bool = True) -> WriteToFile:
    return WriteToFile(ctx=_tool_ctx(launch_root, is_tui=is_tui))


def _orig(launch_root: Path, key: str) -> Path:
    return DiffStore(str(launch_root)).diffs_dir / f"{orig_key(key)}.orig"


# --- TUI gate ---


def test_cli_mode_does_not_track(launch_root):
    """CLI/server: is_tui=False → snapshot no-op, .gc/diffs не создаётся."""
    target = launch_root / "src" / "foo.py"
    target.write_text("baseline\n", encoding="utf-8")
    tool = _write_tool(launch_root, is_tui=False)

    res = json.loads(tool(path=str(target), content="changed\n"))
    assert res["ok"] is True
    assert target.read_text(encoding="utf-8") == "changed\n"
    assert DiffStore(str(launch_root)).load_index() == {}
    assert not (launch_root / ".gc" / "diffs").exists()


# --- T1 / T2 via WriteToFile ---


def test_t1_write_existing_creates_orig(launch_root):
    target = launch_root / "src" / "foo.py"
    target.write_text("baseline\n", encoding="utf-8")
    tool = _write_tool(launch_root)

    res = json.loads(tool(path=str(target), content="changed\n"))
    assert res["ok"] is True
    assert target.read_text(encoding="utf-8") == "changed\n"
    assert _orig(launch_root, "src/foo.py").read_text(encoding="utf-8") == "baseline\n"
    index = DiffStore(str(launch_root)).load_index()
    assert index["src/foo.py"]["status"] == "modified"
    assert index["src/foo.py"]["baseline_exists"] is True


def test_t1_write_new_file_registers_created(launch_root):
    target = launch_root / "src" / "new.py"
    tool = _write_tool(launch_root)

    res = json.loads(tool(path=str(target), content="hello\n"))
    assert res["ok"] is True
    assert not _orig(launch_root, "src/new.py").exists()
    index = DiffStore(str(launch_root)).load_index()
    assert index["src/new.py"]["status"] == "created"
    assert index["src/new.py"]["baseline_exists"] is False


def test_t2_second_write_does_not_overwrite_orig(launch_root):
    target = launch_root / "src" / "foo.py"
    target.write_text("v1\n", encoding="utf-8")
    tool = _write_tool(launch_root)

    assert json.loads(tool(path=str(target), content="v2\n"))["ok"] is True
    assert json.loads(tool(path=str(target), content="v3\n"))["ok"] is True
    assert _orig(launch_root, "src/foo.py").read_text(encoding="utf-8") == "v1\n"


# --- append / apply / search_and_replace ---


def test_append_existing_snapshots_orig(launch_root):
    target = launch_root / "src" / "a.py"
    target.write_text("head\n", encoding="utf-8")
    tool = AppendToFile(ctx=_tool_ctx(launch_root))

    res = json.loads(tool(path=str(target), content="tail", separator="\n"))
    assert res["ok"] is True
    assert _orig(launch_root, "src/a.py").read_text(encoding="utf-8") == "head\n"


def test_search_and_replace_snapshots_orig(launch_root):
    target = launch_root / "src" / "b.py"
    target.write_text("foo = 1\n", encoding="utf-8")
    tool = SearchAndReplace(ctx=_tool_ctx(launch_root))

    res = json.loads(
        tool(path=str(target), search="foo = 1", replace="foo = 2")
    )
    assert res["ok"] is True
    assert _orig(launch_root, "src/b.py").read_text(encoding="utf-8") == "foo = 1\n"
    assert target.read_text(encoding="utf-8") == "foo = 2\n"


def test_search_and_replace_dry_run_does_not_track(launch_root):
    target = launch_root / "src" / "b.py"
    target.write_text("foo = 1\n", encoding="utf-8")
    tool = SearchAndReplace(ctx=_tool_ctx(launch_root))

    res = json.loads(
        tool(path=str(target), search="foo = 1", replace="foo = 2", dry_run=True)
    )
    assert res["ok"] is True
    assert DiffStore(str(launch_root)).load_index() == {}
    assert list(DiffStore(str(launch_root)).diffs_dir.glob("*.orig")) == []


def test_apply_file_edits_snapshots_orig(launch_root):
    target = launch_root / "src" / "c.py"
    target.write_text("alpha\n", encoding="utf-8")
    tool = ApplyFileEdits(ctx=_tool_ctx(launch_root))

    res = json.loads(
        tool(
            path=str(target),
            edits=[{"search": "alpha", "replace": "beta"}],
        )
    )
    assert res["ok"] is True
    assert _orig(launch_root, "src/c.py").read_text(encoding="utf-8") == "alpha\n"


# --- T7 binary ---


def test_t7_binary_write_not_tracked_via_overwrite_of_binary(launch_root):
    """Существующий binary: register_mutation видит is_binary → не трекаем."""
    target = launch_root / "src" / "blob.bin"
    target.write_bytes(b"\x00\x01\xffbinary")
    tool = _write_tool(launch_root)

    res = json.loads(tool(path=str(target), content="now text\n"))
    assert res["ok"] is True
    assert DiffStore(str(launch_root)).load_index() == {}
    assert list(DiffStore(str(launch_root)).diffs_dir.glob("*.orig")) == []


# --- git-mode no-op ---


def test_git_mode_tracking_is_noop_for_tracked(launch_root):
    """git-mode + non-ignored → snapshot no-op (baseline = git index)."""
    if not _has_git():
        pytest.skip("git недоступен")
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=launch_root,
        check=True,
        capture_output=True,
    )
    target = launch_root / "src" / "foo.py"
    target.write_text("base\n", encoding="utf-8")
    tool = _write_tool(launch_root)

    assert json.loads(tool(path=str(target), content="new\n"))["ok"] is True
    assert DiffStore(str(launch_root)).load_index() == {}
    assert not (launch_root / ".gc" / "diffs").exists() or list(
        (launch_root / ".gc" / "diffs").glob("*")
    ) == []


def test_git_mode_ignored_is_tracked_via_cache(launch_root):
    """git-mode + ignored → snapshot в ``.gc/diffs/``."""
    if not _has_git():
        pytest.skip("git недоступен")
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=launch_root,
        check=True,
        capture_output=True,
    )
    (launch_root / ".gitignore").write_text("src/secret.md\n", encoding="utf-8")
    target = launch_root / "src" / "secret.md"
    target.write_text("base\n", encoding="utf-8")
    tool = _write_tool(launch_root)

    assert json.loads(tool(path=str(target), content="new\n"))["ok"] is True
    assert _orig(launch_root, "src/secret.md").read_text(encoding="utf-8") == "base\n"
    assert DiffStore(str(launch_root)).load_index()["src/secret.md"]["status"] == "modified"
