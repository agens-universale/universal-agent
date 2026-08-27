"""Live native find/grep for fs_find_in_files — skipped per missing binary.

Python-canon tests force ``has_grep=False``. Here the host recipes run for real:

* ``has_grep`` → ``grep -H -R …`` (no prune) and the single-file argv
* ``has_find`` (implies the prune pipeline) → ``find -print0 | xargs -0 -r grep``

A box with grep but no find still covers the grep tests; a box without grep
skips the whole module.
"""

from __future__ import annotations

import json

import pytest

from backends.local import LocalFsBackend
from tests.helpers.tool_ctx import with_local_ctx
from tools.fs.find_in_files import FindInFiles
from utils.native_fs_search import run_native_grep
from utils.pattern_native_gate import NativeGrepPlan


def _host_caps():
    backend = LocalFsBackend()
    try:
        grep = bool(backend.has_grep)
    except Exception:
        grep = False
    try:
        find = bool(backend.has_find)
    except Exception:
        find = False
    return grep, find


_HOST_HAS_GREP, _HOST_HAS_FIND = _host_caps()

requires_grep = pytest.mark.skipif(
    not _HOST_HAS_GREP, reason="host grep recipe is not usable"
)
requires_find = pytest.mark.skipif(
    not _HOST_HAS_FIND, reason="host find | xargs | grep pipeline is not usable"
)

PLAN = NativeGrepPlan(fixed=True, pattern="TOKEN", case_insensitive=False)


def _tool(tmp_path, extra=None):
    cfg = {"launch_root": str(tmp_path)}
    if extra:
        cfg["fs_tools"] = {"find_in_files": extra}
    return with_local_ctx(FindInFiles, config_dict=cfg)


def _call_tool(tmp_path, extra=None, **kwargs):
    raw = _tool(tmp_path, extra)(path=str(tmp_path), **kwargs)
    return json.loads(raw) if isinstance(raw, str) else raw


def _warm_caps(backend):
    """Run capability probes before spying so fixture exec is not mixed in."""
    _ = backend.has_grep
    _ = backend.has_find


def _spy_exec(monkeypatch, backend):
    _warm_caps(backend)
    calls: list[tuple[str, object]] = []
    orig_exec = backend.exec
    orig_argv = backend.exec_argv

    def exec_spy(command, **kwargs):
        calls.append(("exec", command))
        return orig_exec(command, **kwargs)

    def argv_spy(argv, **kwargs):
        calls.append(("argv", list(argv)))
        return orig_argv(argv, **kwargs)

    monkeypatch.setattr(backend, "exec", exec_spy)
    monkeypatch.setattr(backend, "exec_argv", argv_spy)
    return calls


# ------------------------------------------------------------------
# find -print0 | xargs -0 -r grep  (directory prune — what the tool uses)
# ------------------------------------------------------------------


@requires_find
def test_dir_search_uses_find_xargs_grep_pipeline(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("TOKEN = 1\n", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.py").write_text("TOKEN = leak\n", encoding="utf-8")

    tool = _tool(tmp_path)
    calls = _spy_exec(monkeypatch, tool._backend)
    data = json.loads(tool(path=str(tmp_path), pattern="TOKEN", query_mode="text"))

    assert data["search_backend"] == "grep"
    assert data["count"] == 1
    assert data["matches"][0]["file"].endswith("app.py")

    piped = [cmd for kind, cmd in calls if kind == "exec" and "| xargs" in str(cmd)]
    assert piped, calls
    assert any("find" in cmd and "-print0" in cmd and "xargs -0 -r" in cmd for cmd in piped)


@requires_find
def test_find_prunes_hard_exclude_dirs(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("TOKEN = 1\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("TOKEN = dep\n", encoding="utf-8")

    tool = _tool(tmp_path)
    calls = _spy_exec(monkeypatch, tool._backend)
    data = json.loads(tool(path=str(tmp_path), pattern="TOKEN", query_mode="text"))

    assert data["search_backend"] == "grep"
    files = [m["file"] for m in data["matches"]]
    assert any(f.endswith("app.py") for f in files)
    assert not any("node_modules" in f for f in files)
    piped = [cmd for kind, cmd in calls if kind == "exec" and "| xargs" in str(cmd)]
    assert any("node_modules" in cmd for cmd in piped)


@requires_find
def test_native_context_is_the_match_line(tmp_path):
    """Native has no ±N window: context is the clipped match line."""
    (tmp_path / "app.py").write_text(
        "\n".join(f"line_{i}" for i in range(1, 21)) + "\n", encoding="utf-8"
    )

    data = _call_tool(tmp_path, pattern="line_10", query_mode="text")

    assert data["search_backend"] == "grep"
    ctx = data["matches"][0]["context"]
    assert "line_10" in ctx
    assert "line_5" not in ctx
    assert "line_15" not in ctx


@requires_find
def test_native_dir_mode_does_not_apply_max_file_bytes(tmp_path):
    """Size ceiling is a Python-walk signal; grep reads the file anyway."""
    (tmp_path / "big.log").write_text("NEEDLE " * 40, encoding="utf-8")
    (tmp_path / "ok.py").write_text("nothing here\n", encoding="utf-8")

    data = _call_tool(
        tmp_path, {"max_file_bytes": 50}, pattern="NEEDLE", query_mode="text"
    )

    assert data["search_backend"] == "grep"
    assert data["count"] >= 1
    assert "files_too_large" not in data
    assert any(m["file"].endswith("big.log") for m in data["matches"])


@requires_find
def test_native_clips_long_line_to_the_head_budget(tmp_path):
    """Without match offsets grep clips the first N chars (Python keeps the hit)."""
    (tmp_path / "one.txt").write_text(
        "a" * 3000 + "NEEDLE" + "b" * 3000, encoding="utf-8"
    )

    data = _call_tool(tmp_path, pattern="NEEDLE", query_mode="text")

    assert data["search_backend"] == "grep"
    assert data["count"] == 1
    line = data["matches"][0]["line_content"]
    assert len(line) <= FindInFiles.max_line_content_chars + 80
    assert "line_truncated" in line


# ------------------------------------------------------------------
# grep -H … without find (recursive -R and single file)
# ------------------------------------------------------------------


@requires_grep
def test_recursive_grep_recipe_without_find_prune(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("TOKEN = 1\n", encoding="utf-8")
    backend = LocalFsBackend()
    calls = _spy_exec(monkeypatch, backend)

    result = run_native_grep(
        backend,
        str(tmp_path),
        PLAN,
        exclude_dir_names=[],
        exclude_nested_dot_dirs=False,
        file_pattern=None,
        max_matches=10,
        report_path=lambda p: p,
        is_dir_target=True,
    )

    assert result is not None
    assert any(m["file"].endswith("app.py") for m in result.matches)
    argv_calls = [argv for kind, argv in calls if kind == "argv"]
    assert argv_calls, calls
    grep_argv = argv_calls[-1]
    assert grep_argv[0] == "grep"
    assert "-H" in grep_argv
    assert "-R" in grep_argv
    assert not any(kind == "exec" and "| xargs" in str(cmd) for kind, cmd in calls)


@requires_grep
def test_single_file_grep_recipe(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    target.write_text("TOKEN = 1\n", encoding="utf-8")
    backend = LocalFsBackend()
    calls = _spy_exec(monkeypatch, backend)

    result = run_native_grep(
        backend,
        str(target),
        PLAN,
        exclude_dir_names=[],
        exclude_nested_dot_dirs=False,
        file_pattern=None,
        max_matches=10,
        report_path=lambda p: p,
        is_dir_target=False,
    )

    assert result is not None
    assert result.matches
    assert result.matches[0]["line"] == 1
    argv_calls = [argv for kind, argv in calls if kind == "argv"]
    assert argv_calls, calls
    grep_argv = argv_calls[-1]
    assert grep_argv[0] == "grep"
    assert "-H" in grep_argv
    assert "-R" not in grep_argv
    assert str(target) in grep_argv
