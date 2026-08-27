"""TrackingFsBackend: snapshot + journal через обёртку Local (spec S11).

Тулы не импортируют diff_tui; только TUI + thread identity.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from config_modules import env_vars
from diff_tui.paths import orig_key
from diff_tui.store import DiffStore
from diff_tui.touched import TouchedJournal
from diff_tui.tracking import TrackingFsBackend, wrap_local_for_tui
from execution_workspace import build_execution_workspace
from tests.helpers.tool_ctx import local_ctx
from backends.local import LocalFsBackend
from backends.protocol import FsBackend
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
    threads = tmp_path / "threads"
    threads.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    monkeypatch.chdir(root)
    return root, threads


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


def _tool_ctx(
    launch_root: Path,
    threads: Path,
    *,
    is_tui: bool = True,
    thread_id: str = "t1",
):
    cfg = _cfg(launch_root)
    ctx = local_ctx(cfg)
    ctx.is_tui = is_tui
    if is_tui:
        ctx.threads_dir = str(threads)
        ctx.system_id = "tui"
        ctx.thread_id = thread_id
        wrapped = wrap_local_for_tui(ctx.os_backend, ctx)
        ctx.os_backend = wrapped
        ctx.workspace = build_execution_workspace(wrapped, cfg)
    return ctx


def _write_tool(launch_root: Path, threads: Path, **kw) -> WriteToFile:
    return WriteToFile(ctx=_tool_ctx(launch_root, threads, **kw))


def _orig(launch_root: Path, key: str) -> Path:
    return DiffStore(str(launch_root)).diffs_dir / f"{orig_key(key)}.orig"


def test_cli_mode_does_not_track(launch_root):
    root, threads = launch_root
    target = root / "src" / "foo.py"
    target.write_text("baseline\n", encoding="utf-8")
    tool = _write_tool(root, threads, is_tui=False)

    res = json.loads(tool(path=str(target), content="changed\n"))
    assert res["ok"] is True
    assert DiffStore(str(root)).load_index() == {}
    assert not (root / ".gc" / "diffs").exists()
    assert TouchedJournal(str(threads), "tui", "t1").paths() == set()


def test_t1_write_existing_creates_orig(launch_root):
    root, threads = launch_root
    target = root / "src" / "foo.py"
    target.write_text("baseline\n", encoding="utf-8")
    tool = _write_tool(root, threads)

    res = json.loads(tool(path=str(target), content="changed\n"))
    assert res["ok"] is True
    assert _orig(root, "src/foo.py").read_text(encoding="utf-8") == "baseline\n"
    index = DiffStore(str(root)).load_index()
    assert index["src/foo.py"]["status"] == "modified"
    assert "src/foo.py" in TouchedJournal(str(threads), "tui", "t1").paths()


def test_t1_write_new_file_registers_created(launch_root):
    root, threads = launch_root
    target = root / "src" / "new.py"
    tool = _write_tool(root, threads)

    res = json.loads(tool(path=str(target), content="hello\n"))
    assert res["ok"] is True
    assert not _orig(root, "src/new.py").exists()
    index = DiffStore(str(root)).load_index()
    assert index["src/new.py"]["status"] == "created"
    assert "src/new.py" in TouchedJournal(str(threads), "tui", "t1").paths()


def test_t2_second_write_does_not_overwrite_orig(launch_root):
    root, threads = launch_root
    target = root / "src" / "foo.py"
    target.write_text("v1\n", encoding="utf-8")
    tool = _write_tool(root, threads)

    assert json.loads(tool(path=str(target), content="v2\n"))["ok"] is True
    assert json.loads(tool(path=str(target), content="v3\n"))["ok"] is True
    assert _orig(root, "src/foo.py").read_text(encoding="utf-8") == "v1\n"


def test_append_existing_snapshots_orig(launch_root):
    root, threads = launch_root
    target = root / "src" / "a.py"
    target.write_text("head\n", encoding="utf-8")
    tool = AppendToFile(ctx=_tool_ctx(root, threads))

    res = json.loads(tool(path=str(target), content="tail", separator="\n"))
    assert res["ok"] is True
    assert _orig(root, "src/a.py").read_text(encoding="utf-8") == "head\n"


def test_search_and_replace_snapshots_orig(launch_root):
    root, threads = launch_root
    target = root / "src" / "b.py"
    target.write_text("foo = 1\n", encoding="utf-8")
    tool = SearchAndReplace(ctx=_tool_ctx(root, threads))

    res = json.loads(
        tool(path=str(target), search="foo = 1", replace="foo = 2")
    )
    assert res["ok"] is True
    assert _orig(root, "src/b.py").read_text(encoding="utf-8") == "foo = 1\n"


def test_search_and_replace_dry_run_does_not_track(launch_root):
    root, threads = launch_root
    target = root / "src" / "b.py"
    target.write_text("foo = 1\n", encoding="utf-8")
    tool = SearchAndReplace(ctx=_tool_ctx(root, threads))

    res = json.loads(
        tool(path=str(target), search="foo = 1", replace="foo = 2", dry_run=True)
    )
    assert res["ok"] is True
    assert DiffStore(str(root)).load_index() == {}
    assert TouchedJournal(str(threads), "tui", "t1").paths() == set()


def test_apply_file_edits_snapshots_orig(launch_root):
    root, threads = launch_root
    target = root / "src" / "c.py"
    target.write_text("alpha\n", encoding="utf-8")
    tool = ApplyFileEdits(ctx=_tool_ctx(root, threads))

    res = json.loads(
        tool(
            path=str(target),
            edits=[{"search": "alpha", "replace": "beta"}],
        )
    )
    assert res["ok"] is True
    assert _orig(root, "src/c.py").read_text(encoding="utf-8") == "alpha\n"


def test_t7_binary_write_not_tracked_via_overwrite_of_binary(launch_root):
    root, threads = launch_root
    target = root / "src" / "blob.bin"
    target.write_bytes(b"\x00\x01\xffbinary")
    tool = _write_tool(root, threads)

    res = json.loads(tool(path=str(target), content="now text\n"))
    assert res["ok"] is True
    assert DiffStore(str(root)).load_index() == {}


def test_git_mode_tracked_adds_journal_not_cache(launch_root):
    root, threads = launch_root
    if not _has_git():
        pytest.skip("git недоступен")
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    target = root / "src" / "foo.py"
    target.write_text("base\n", encoding="utf-8")
    tool = _write_tool(root, threads)

    assert json.loads(tool(path=str(target), content="new\n"))["ok"] is True
    assert DiffStore(str(root)).load_index() == {}
    assert "src/foo.py" in TouchedJournal(str(threads), "tui", "t1").paths()


def test_git_mode_ignored_is_tracked_via_cache(launch_root):
    root, threads = launch_root
    if not _has_git():
        pytest.skip("git недоступен")
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (root / ".gitignore").write_text("src/secret.md\n", encoding="utf-8")
    target = root / "src" / "secret.md"
    target.write_text("base\n", encoding="utf-8")
    tool = _write_tool(root, threads)

    assert json.loads(tool(path=str(target), content="new\n"))["ok"] is True
    assert _orig(root, "src/secret.md").read_text(encoding="utf-8") == "base\n"
    assert "src/secret.md" in TouchedJournal(str(threads), "tui", "t1").paths()


def test_exec_delta_adds_journal(launch_root):
    root, threads = launch_root
    if not _has_git():
        pytest.skip("git недоступен")
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    ctx = _tool_ctx(root, threads)
    assert isinstance(ctx.os_backend, TrackingFsBackend)
    ctx.os_backend.exec("echo hello > shell_out.txt", cwd=str(root))
    assert "shell_out.txt" in TouchedJournal(str(threads), "tui", "t1").paths()


def test_wrap_skips_non_local():
    class HarborLike:
        pass

    ctx = type("C", (), {"is_tui": True})()
    sentinel = HarborLike()
    assert wrap_local_for_tui(sentinel, ctx) is sentinel


def test_tools_do_not_import_diff_tui():
    import tools.fs.write_to_file_tool as w
    import tools.fs.append_to_file as a
    import tools.fs.search_and_replace as s
    import tools.fs.apply_file_edits as e

    for mod in (w, a, s, e):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "diff_tui" not in src


# --- wrapper parity with the FsBackend port ---------------------------------

# Members of FsBackend the wrapper is allowed NOT to expose. Keep it empty:
# TrackingFsBackend is a decorator, so every public member of the port must be
# reachable through it (it is not an FsBackend subclass, so ABC cannot enforce).
TRACKING_NOT_FORWARDED: set[str] = set()


def _fsbackend_public_members() -> set[str]:
    """Public methods/properties/attrs declared on the FsBackend port itself."""
    return {name for name in vars(FsBackend) if not name.startswith("_")}


def test_tracking_fs_backend_mirrors_fsbackend_public_surface():
    """Structural guard: adding a member to FsBackend must fail until the
    TUI decorator forwards it.

    Method-specific regressions (exec_argv, then has_grep/has_find) kept
    slipping through because nothing enumerated the port's surface.
    """
    wrapper = TrackingFsBackend(LocalFsBackend(), local_ctx())
    # Class dict covers methods/properties; instance dict covers attrs copied
    # in __init__ (is_sandbox, default_exec_timeout). Neither reads a property.
    exposed = set(dir(type(wrapper))) | set(vars(wrapper))
    missing = sorted(_fsbackend_public_members() - exposed - TRACKING_NOT_FORWARDED)
    assert missing == []


class _CapStub:
    """Inner backend with fixed capability flags (no subprocess probes)."""

    def __init__(self, value: bool) -> None:
        self.has_find = value
        self.has_grep = value
        self.grep_exclude_dir = value
        self.grep_include = value


def test_capability_flags_true_are_visible_through_wrapper():
    wrapper = TrackingFsBackend(_CapStub(True), local_ctx())
    assert wrapper.has_find is True
    assert wrapper.has_grep is True
    assert wrapper.grep_exclude_dir is True
    assert wrapper.grep_include is True


def test_capability_flags_false_stay_false_through_wrapper():
    wrapper = TrackingFsBackend(_CapStub(False), local_ctx())
    assert wrapper.has_find is False
    assert wrapper.has_grep is False
    assert wrapper.grep_exclude_dir is False
    assert wrapper.grep_include is False
