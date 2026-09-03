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

from gc_core.backends.local import LocalFsBackend
from gc_testkit.helpers.tool_ctx import with_local_ctx
from gc_core.tools.fs.find_in_files import FindInFiles
from gc_core.utils.native_fs_search import run_native_grep
from gc_core.utils.pattern_native_gate import NativeGrepPlan


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
# Пайплайн find | xargs | grep требует ОБА бинаря: тесты ниже утверждают
# search_backend == "grep", поэтому без grep они не «падают», а не применимы
# (на Windows find из Git Bash есть, GNU grep нет — движок честно уходит в Python).
requires_native_search = pytest.mark.skipif(
    not (_HOST_HAS_FIND and _HOST_HAS_GREP),
    reason="host find | xargs | grep pipeline is not usable",
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


@requires_native_search
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


@requires_native_search
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


@requires_native_search
def test_native_context_is_a_plus_minus_five_window(tmp_path):
    """After grep hits, context is the ±5 neighborhood, not a match-line duplicate."""
    (tmp_path / "app.py").write_text(
        "\n".join(f"line_{i}" for i in range(1, 21)) + "\n", encoding="utf-8"
    )

    data = _call_tool(tmp_path, pattern="line_10", query_mode="text")

    assert data["search_backend"] == "grep"
    match = data["matches"][0]
    ctx = match["context"]
    assert "line_10" in ctx
    assert "line_5" in ctx
    assert "line_15" in ctx
    assert "line_4" not in ctx
    assert "line_16" not in ctx
    assert ctx != match["line_content"]
    assert ctx != match["line_content"] + "\n"


@requires_native_search
def test_native_dir_mode_omits_context_when_file_exceeds_max_bytes(tmp_path, monkeypatch):
    """Grep still locates the hit; oversize files skip the download for the window."""
    (tmp_path / "big.log").write_text("NEEDLE " * 40, encoding="utf-8")
    (tmp_path / "ok.py").write_text("nothing here\n", encoding="utf-8")

    tool = _tool(tmp_path, {"max_file_bytes": 50})
    reads: list[str] = []
    orig_read = tool._backend.read_bytes

    def _read(path):
        reads.append(path)
        return orig_read(path)

    monkeypatch.setattr(tool._backend, "read_bytes", _read)
    data = json.loads(tool(path=str(tmp_path), pattern="NEEDLE", query_mode="text"))

    assert data["search_backend"] == "grep"
    assert data["count"] >= 1
    assert "files_too_large" not in data
    hit = next(m for m in data["matches"] if m["file"].endswith("big.log"))
    assert "NEEDLE" in hit["line_content"]
    assert "context" not in hit
    assert hit["context_omitted"] == "file_too_large"
    assert hit["size_bytes"] > 50
    assert reads == []


@requires_native_search
def test_native_context_downloads_each_shown_file_once(tmp_path, monkeypatch):
    body = "\n".join(
        ["alpha", "TOKEN first", "beta", "gamma", "TOKEN second", "delta"]
    ) + "\n"
    (tmp_path / "app.py").write_text(body, encoding="utf-8")

    tool = _tool(tmp_path)
    reads: list[str] = []
    sizes: list[str] = []
    orig_read = tool._backend.read_bytes
    orig_size = tool._backend.getsize

    def _read(path):
        reads.append(path)
        return orig_read(path)

    def _size(path):
        sizes.append(path)
        return orig_size(path)

    monkeypatch.setattr(tool._backend, "read_bytes", _read)
    monkeypatch.setattr(tool._backend, "getsize", _size)
    data = json.loads(tool(path=str(tmp_path), pattern="TOKEN", query_mode="text"))

    assert data["search_backend"] == "grep"
    assert data["count"] == 2
    assert all("context" in m for m in data["matches"])
    assert all("TOKEN" in m["context"] for m in data["matches"])
    assert len(reads) == 1
    assert len(sizes) == 1


@requires_native_search
def test_native_context_omitted_on_read_error(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("TOKEN = 1\n", encoding="utf-8")
    tool = _tool(tmp_path)
    orig_size = tool._backend.getsize

    def _size(path):
        return orig_size(path)

    def _read(_path):
        raise OSError("boom")

    monkeypatch.setattr(tool._backend, "getsize", _size)
    monkeypatch.setattr(tool._backend, "read_bytes", _read)
    data = json.loads(tool(path=str(tmp_path), pattern="TOKEN", query_mode="text"))

    assert data["search_backend"] == "grep"
    assert data["count"] == 1
    hit = data["matches"][0]
    assert "TOKEN" in hit["line_content"]
    assert "context" not in hit
    assert hit["context_omitted"] == "read_error"
    assert "size_bytes" in hit


@requires_native_search
def test_native_context_omitted_on_stat_error(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("TOKEN = 1\n", encoding="utf-8")
    tool = _tool(tmp_path)

    def _size(_path):
        raise OSError("stat failed")

    reads: list[str] = []

    def _read(path):
        reads.append(path)
        raise AssertionError("read_bytes must not run after getsize failure")

    monkeypatch.setattr(tool._backend, "getsize", _size)
    monkeypatch.setattr(tool._backend, "read_bytes", _read)
    data = json.loads(tool(path=str(tmp_path), pattern="TOKEN", query_mode="text"))

    assert data["search_backend"] == "grep"
    assert data["count"] == 1
    hit = data["matches"][0]
    assert "context" not in hit
    assert hit["context_omitted"] == "read_error"
    assert "size_bytes" not in hit
    assert reads == []


@requires_native_search
def test_native_context_omitted_on_binary(tmp_path):
    (tmp_path / "app.bin").write_bytes(b"TOKEN\x00binary\n")
    data = _call_tool(tmp_path, pattern="TOKEN", query_mode="text")

    assert data["search_backend"] == "grep"
    if data["count"] == 0:
        pytest.skip("host grep skipped the NUL file")
    hit = data["matches"][0]
    assert "context" not in hit
    assert hit["context_omitted"] == "binary"
    assert "size_bytes" in hit


@requires_native_search
def test_native_context_omitted_on_encoding(tmp_path):
    (tmp_path / "legacy.txt").write_bytes("TOKEN привет\n".encode("cp1251"))
    data = _call_tool(tmp_path, pattern="TOKEN", query_mode="text")

    assert data["search_backend"] == "grep"
    assert data["count"] == 1
    hit = data["matches"][0]
    assert "TOKEN" in hit["line_content"]
    assert "context" not in hit
    assert hit["context_omitted"] == "encoding"
    assert "size_bytes" in hit


@requires_native_search
def test_native_context_clips_long_lines_to_budget(tmp_path):
    huge = "x" * 3000
    lines = [f"pad_{i}" for i in range(1, 10)]
    lines[4] = huge + "NEEDLE" + huge
    (tmp_path / "one.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    data = _call_tool(tmp_path, pattern="NEEDLE", query_mode="text")

    assert data["search_backend"] == "grep"
    assert data["count"] == 1
    ctx = data["matches"][0]["context"]
    assert "NEEDLE" in ctx or "line_truncated" in ctx or "context_truncated" in ctx
    assert len(ctx) <= FindInFiles.max_context_chars


@requires_native_search
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


@requires_grep
@pytest.mark.skipif(__import__("sys").platform == "win32", reason="GNU grep семантика; на Windows native выключен")
def test_native_finds_ascii_needle_in_a_cp1251_file(tmp_path):
    """В UTF-8-локали grep считал cp1251-файл бинарным и молча пропускал — ответ объявлялся полным."""
    (tmp_path / "legacy.txt").write_bytes("needle привет\n".encode("cp1251"))
    tool = with_local_ctx(FindInFiles, config_dict={"launch_root": str(tmp_path)})

    data = json.loads(tool(path=str(tmp_path), pattern="needle", query_mode="text"))

    assert data["search_backend"] == "grep"
    assert data["count"] == 1
