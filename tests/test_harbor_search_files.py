"""Differential tests: SearchFiles over FakeBackend vs SearchFiles over a real temp tree.

Strategy:
- Build a real temp dir tree for one run of the base tool (LocalFsBackend, via ctx fallback).
- Mirror the same files in FakeBackend for the other run (explicit `_backend` override).
- Run the SAME tool class with the same kwargs against both.
- Compare structured output, normalizing absolute path roots (base=real tmpdir, shim=/work).
"""
import json
import os
import re
from types import SimpleNamespace

import pytest

from tests.harbor_support.fakes import FakeBackend, bind_os_backend
from gc_core.backends.local import LocalFsBackend
from gc_core.tools.fs.search_files import SearchFiles

# ---------------------------------------------------------------------------
# Tree fixture
# ---------------------------------------------------------------------------
TREE = {
    # relpath -> content
    "a.py": "hello world\ndef foo():\n    pass\n",
    "b.txt": "no match here\njust text\n",
    "sub/c.py": "def bar():\n    return 42\nhello from sub\n",
    "sub/d.log": "error: something went wrong\n",
    "sub/nested/e.py": "hello nested\n",
}

SHIM_ROOT = "/work"


def _make_real_tree(tmp_path):
    """Write TREE to a real temp dir; return root path."""
    for relpath, content in TREE.items():
        full = tmp_path / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    return str(tmp_path)


def _make_fake_backend():
    files = {f"{SHIM_ROOT}/{k}": v for k, v in TREE.items()}
    return FakeBackend(files)


def _base_tool():
    t = SearchFiles.__new__(SearchFiles)
    return bind_os_backend(t, LocalFsBackend())


def _shim_tool():
    t = SearchFiles.__new__(SearchFiles)
    return bind_os_backend(t, FakeBackend(_make_fake_backend().files))


def _normalize_file(path: str, root: str) -> str:
    """Strip root prefix from a file path, returning a relpath."""
    root = root.rstrip("/")
    if path.startswith(root + "/"):
        return path[len(root) + 1:]
    if path.startswith(root):
        return path[len(root):]
    return path


def _normalize_matches(matches, root):
    normed = []
    for m in matches:
        m2 = dict(m)
        if "file" in m2:
            m2["file"] = _normalize_file(m2["file"], root)
        normed.append(m2)
    return sorted(normed, key=lambda m: (m.get("file", ""), m.get("line", 0)))


def _run_both(tmp_path, **kwargs):
    """Return (base_env, shim_env) as parsed dicts."""
    real_root = _make_real_tree(tmp_path)

    base = _base_tool()
    base_raw = base(path=real_root, **kwargs)
    base_env = json.loads(base_raw)

    shim = _shim_tool()
    shim_raw = shim(path=SHIM_ROOT, **kwargs)
    shim_env = json.loads(shim_raw)

    return base_env, shim_env, real_root


def _compare(base_env, shim_env, real_root, *, check_pagination=True):
    """Assert structural equality after normalizing paths."""
    assert base_env["ok"] == shim_env["ok"], (
        f"ok mismatch: base={base_env['ok']} shim={shim_env['ok']}"
    )
    if not base_env["ok"]:
        # Error cases: compare error type
        b_err = base_env.get("error") or {}
        s_err = shim_env.get("error") or {}
        assert b_err.get("type") == s_err.get("type"), (
            f"error.type mismatch: {b_err.get('type')} vs {s_err.get('type')}"
        )
        return

    # Normalize matches
    b_matches = _normalize_matches(base_env.get("matches", []), real_root)
    s_matches = _normalize_matches(shim_env.get("matches", []), SHIM_ROOT)
    assert b_matches == s_matches, f"matches differ:\nbase={b_matches}\nshim={s_matches}"

    assert base_env.get("total_matches") == shim_env.get("total_matches"), (
        f"total_matches: {base_env.get('total_matches')} vs {shim_env.get('total_matches')}"
    )
    assert base_env.get("count") == shim_env.get("count"), (
        f"count: {base_env.get('count')} vs {shim_env.get('count')}"
    )
    assert base_env.get("truncated") == shim_env.get("truncated"), (
        f"truncated: {base_env.get('truncated')} vs {shim_env.get('truncated')}"
    )
    if check_pagination:
        assert base_env.get("offset") == shim_env.get("offset"), (
            f"offset: {base_env.get('offset')} vs {shim_env.get('offset')}"
        )
        assert base_env.get("limit") == shim_env.get("limit"), (
            f"limit: {base_env.get('limit')} vs {shim_env.get('limit')}"
        )
        # next_offset may differ since match order can vary across walks, but presence/absence must match
        b_next = base_env.get("next_offset")
        s_next = shim_env.get("next_offset")
        assert (b_next is None) == (s_next is None), (
            f"next_offset presence: base={b_next} shim={s_next}"
        )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_basic_regex(tmp_path):
    """Matches 'hello' across multiple files."""
    base_env, shim_env, real_root = _run_both(tmp_path, regex="hello")
    _compare(base_env, shim_env, real_root)
    # Sanity: there are matches
    assert base_env["total_matches"] > 0


def test_file_pattern_glob(tmp_path):
    """file_pattern='*.py' restricts to Python files only."""
    base_env, shim_env, real_root = _run_both(tmp_path, regex="hello", file_pattern="*.py")
    _compare(base_env, shim_env, real_root)
    # All returned files should end with .py
    for m in shim_env.get("matches", []):
        assert m["file"].endswith(".py"), f"non-py file in matches: {m['file']}"


def test_file_pattern_recursive_glob(tmp_path):
    """file_pattern='**/*.py' matches Python files at any depth."""
    base_env, shim_env, real_root = _run_both(tmp_path, regex="def", file_pattern="**/*.py")
    _compare(base_env, shim_env, real_root)
    assert base_env["total_matches"] > 0


def test_no_matches(tmp_path):
    """Regex that matches nothing returns empty matches list."""
    base_env, shim_env, real_root = _run_both(tmp_path, regex="ZZZNOMATCHZZZ")
    _compare(base_env, shim_env, real_root)
    assert base_env["total_matches"] == 0
    assert shim_env["total_matches"] == 0
    assert shim_env["matches"] == []


def test_offset_limit_pagination(tmp_path):
    """Pagination: offset=1, limit=2 returns only first 2 matches."""
    # First get total count
    base_all, shim_all, real_root = _run_both(tmp_path, regex="hello")
    total = base_all["total_matches"]
    if total < 2:
        pytest.skip("Not enough matches for pagination test")

    base_env, shim_env, real_root = _run_both(tmp_path, regex="hello", offset=1, limit=2)
    _compare(base_env, shim_env, real_root)
    assert shim_env["count"] == 2
    assert shim_env["offset"] == 1
    assert shim_env["limit"] == 2
    # If there are more than 2 total matches, should be truncated
    if total > 2:
        assert shim_env["truncated"] is True
        assert shim_env["next_offset"] == 3


def test_long_line_truncation(tmp_path):
    """Lines > 1000 chars trigger truncation in line_content."""
    long_line = "A" * 800 + "FINDME" + "B" * 800
    short_line = "normal line\n"
    content = long_line + "\n" + short_line

    # Override the tree with a special file
    special_file = tmp_path / "long.txt"
    special_file.write_text(content, encoding="utf-8")

    # Run base on the single file's parent
    base = _base_tool()
    base_raw = base(path=str(tmp_path), regex="FINDME", file_pattern="*.txt")
    base_env = json.loads(base_raw)

    # Run shim with same content
    fake = FakeBackend({f"{SHIM_ROOT}/long.txt": content})
    shim = bind_os_backend(SearchFiles.__new__(SearchFiles), fake)
    shim_raw = shim(path=SHIM_ROOT, regex="FINDME", file_pattern="*.txt")
    shim_env = json.loads(shim_raw)

    assert base_env["ok"] == shim_env["ok"]
    assert base_env["total_matches"] == shim_env["total_matches"]

    # Both should truncate long lines with same cutoff message structure
    if base_env["matches"]:
        b_content = base_env["matches"][0]["line_content"]
        s_content = shim_env["matches"][0]["line_content"]
        # Both should contain FINDME or indicate it was found
        # The line truncation key is the cutted indicator
        assert "FINDME" in s_content or "cutted" in s_content.lower()
        # Same truncation behavior: both use 500-char context window around match
        assert b_content == s_content, (
            f"line_content differs:\nbase={b_content!r}\nshim={s_content!r}"
        )


def _assert_invalid_regex_envelope(env: dict, pattern: str) -> None:
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert env["error"]["field"] == "regex"
    assert env["error"]["message"].startswith("Invalid regex:")
    assert env["matches"] == []
    assert env["count"] == 0
    assert env["regex"] == pattern


def test_invalid_regex(tmp_path):
    """Invalid regex: SearchFiles returns ok=False with error.type=invalid_argument.

    Regex is validated upfront in ``__call__`` before walking files, so both
    LocalFsBackend and an injected FakeBackend return the same envelope.
    """
    real_root = _make_real_tree(tmp_path)
    pattern = "[invalid("

    base = _base_tool()
    base_env = json.loads(base(path=real_root, regex=pattern))
    _assert_invalid_regex_envelope(base_env, pattern)

    shim = _shim_tool()
    shim_env = json.loads(shim(path=SHIM_ROOT, regex=pattern))
    _assert_invalid_regex_envelope(shim_env, pattern)


def test_invalid_regex_empty_tree(tmp_path):
    """Invalid regex is rejected even when the search root has no files."""
    empty = tmp_path / "empty"
    empty.mkdir()
    pattern = "("

    base = _base_tool()
    base_env = json.loads(base(path=str(empty), regex=pattern))
    _assert_invalid_regex_envelope(base_env, pattern)


def test_search_single_file_path(tmp_path):
    """Passing a file path searches that file (local + harbor shim)."""
    f = tmp_path / "file.txt"
    f.write_text("hello content\nother\n", encoding="utf-8")

    base = _base_tool()
    base_env = json.loads(base(path=str(f), regex="content"))

    fake = FakeBackend({"/work/file.txt": "hello content\nother\n"})
    shim = bind_os_backend(SearchFiles.__new__(SearchFiles), fake)
    shim_env = json.loads(shim(path="/work/file.txt", regex="content"))

    assert base_env["ok"] is True
    assert shim_env["ok"] is True
    assert base_env["total_matches"] == 1
    assert shim_env["total_matches"] == 1
    assert base_env["matches"][0]["line"] == 1
    assert shim_env["matches"][0]["line"] == 1
    assert "content" in base_env["matches"][0]["line_content"]
    assert "content" in shim_env["matches"][0]["line_content"]


# ---------------------------------------------------------------------------
# Truncated-page ordering test (faithfulness of DFS walk order)
# ---------------------------------------------------------------------------

# A branching tree where os.walk DFS and FakeBackend.walk flat-alphabetical
# produce DIFFERENT orderings of matched files.
#
# os.walk DFS top-down visits (dirs first, then files, then recurse into subdirs):
#   root/
#     alpha/      <- dir emitted, then recurse before gamma/
#       x.py      <- file in alpha/ emitted first
#       beta/     <- dir in alpha/
#         z.md    <- file emitted after recursing into alpha/beta/
#     gamma/      <- second top-level dir
#       y.txt     <- file in gamma/
#
# DFS visit order of files: alpha/x.py, alpha/beta/z.md, gamma/y.txt
#
# FakeBackend.walk returns: sorted dirs then sorted files (flat), so files come as:
#   alpha/beta/z.md, alpha/x.py, gamma/y.txt   <- alphabetical, NOT DFS
#
# With limit=1 (only the first match in the window), the page content differs:
#   - base page: match from alpha/x.py
#   - shim (bug): match from alpha/beta/z.md
BRANCHING_TREE = {
    # Each file has exactly one line that matches "MARKER"
    "alpha/x.py": "MARKER first in alpha\nother stuff\n",
    "alpha/beta/z.md": "MARKER nested in alpha/beta\nmore text\n",
    "gamma/y.txt": "MARKER in gamma\nend\n",
}


def _make_real_branching_tree(tmp_path):
    for relpath, content in BRANCHING_TREE.items():
        full = tmp_path / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    return str(tmp_path)


def _make_fake_branching_backend():
    files = {f"{SHIM_ROOT}/{k}": v for k, v in BRANCHING_TREE.items()}
    return FakeBackend(files)


def _run_both_branching(tmp_path, **kwargs):
    real_root = _make_real_branching_tree(tmp_path)

    base = _base_tool()
    base_raw = base(path=real_root, **kwargs)
    base_env = json.loads(base_raw)

    shim = bind_os_backend(SearchFiles.__new__(SearchFiles), _make_fake_branching_backend())
    shim_raw = shim(path=SHIM_ROOT, **kwargs)
    shim_env = json.loads(shim_raw)

    return base_env, shim_env, real_root


def _normalize_matches_ordered(matches, root):
    """Normalize paths in match list but PRESERVE ORDER (do NOT sort)."""
    normed = []
    for m in matches:
        m2 = dict(m)
        if "file" in m2:
            m2["file"] = _normalize_file(m2["file"], root)
        normed.append(m2)
    return normed


def test_truncated_page_ordering(tmp_path):
    """Truncated page must enumerate files in sorted DFS (os.walk top-down) order.

    Uses a branching tree where FakeBackend.walk (flat alphabetical) and sorted DFS
    produce DIFFERENT orderings on a truncated page — catching the faithfulness bug.

    Sorted DFS (files-then-recurse-into-subdirs at each level):
      root → (no root-level files) → recurse alpha → recurse gamma
        alpha: files [x.py], subdirs [beta] → yield alpha/x.py, recurse beta
          beta: files [z.md] → yield alpha/beta/z.md
        gamma: files [y.txt] → yield gamma/y.txt
      File visit order: alpha/x.py (0), alpha/beta/z.md (1), gamma/y.txt (2)

    FakeBackend.walk flat-alphabetical file order (the BUG):
      alpha/beta/z.md (0), alpha/x.py (1), gamma/y.txt (2)

    With offset=1, limit=1:
      - Sorted DFS shim: page contains alpha/beta/z.md (position 1)
      - Buggy flat-walk shim: page contains alpha/x.py (position 1)  ← wrong
    """
    shim = bind_os_backend(SearchFiles.__new__(SearchFiles), _make_fake_branching_backend())

    # Sanity: 3 total matches (one per file), all match "MARKER"
    shim_all_raw = shim(path=SHIM_ROOT, regex="MARKER")
    shim_all = json.loads(shim_all_raw)
    assert shim_all["ok"] is True, f"shim all-matches failed: {shim_all}"
    assert shim_all["total_matches"] == 3, (
        f"expected 3 total matches, got {shim_all['total_matches']}"
    )

    # All 3 matches should be in sorted DFS order when no pagination is applied
    full_page = _normalize_matches_ordered(shim_all["matches"], SHIM_ROOT)
    full_files = [m["file"] for m in full_page]
    assert full_files == ["alpha/x.py", "alpha/beta/z.md", "gamma/y.txt"], (
        f"Expected sorted DFS order [alpha/x.py, alpha/beta/z.md, gamma/y.txt], "
        f"got {full_files}\n"
        f"(Bug: flat-alphabetical walk gives [alpha/beta/z.md, alpha/x.py, gamma/y.txt])"
    )

    # Verify total_matches matches the base oracle
    base_all, _, real_root = _run_both_branching(tmp_path, regex="MARKER")
    assert base_all["ok"] is True, f"base failed: {base_all}"
    assert base_all["total_matches"] == shim_all["total_matches"], (
        f"total_matches: base={base_all['total_matches']} shim={shim_all['total_matches']}"
    )

    # Now request a TRUNCATED page: limit=1, offset=2 (position 1 in 0-based = index 1)
    # In sorted DFS the match at position 1 (offset=2 gives index=1) is alpha/beta/z.md.
    # The buggy flat-walk shim would return alpha/x.py there instead.
    shim_page_raw = shim(path=SHIM_ROOT, regex="MARKER", offset=2, limit=1)
    shim_page = json.loads(shim_page_raw)

    assert shim_page["ok"] is True, f"shim paginated request failed: {shim_page}"
    assert shim_page["total_matches"] == 3
    assert shim_page["count"] == 1
    assert shim_page["truncated"] is True
    assert shim_page["next_offset"] is not None

    page_matches = _normalize_matches_ordered(shim_page["matches"], SHIM_ROOT)
    assert len(page_matches) == 1
    page_file = page_matches[0]["file"]
    assert page_file == "alpha/beta/z.md", (
        f"At offset=2 limit=1, sorted DFS expects 'alpha/beta/z.md' (position 1, 0-based),\n"
        f"but shim returned '{page_file}'.\n"
        f"  Sorted DFS order: alpha/x.py (0), alpha/beta/z.md (1), gamma/y.txt (2)\n"
        f"  Flat-alphabetical order: alpha/beta/z.md (0), alpha/x.py (1), gamma/y.txt (2)"
    )

    # next_offset: we fetched position 1, position 2 is next → next_offset = 3
    assert shim_page["next_offset"] == 3, (
        f"next_offset should be 3 (there is 1 more match after position 1), "
        f"got {shim_page['next_offset']}"
    )


# ---------------------------------------------------------------------------
# Read-guard regression: denied dirs must be pruned from recursion, not just
# filtered from the emitted file list (Task 6 precedent for fs_list_files).
# ---------------------------------------------------------------------------

def test_walk_does_not_descend_into_denied_dir_via_nested_override():
    """A guard-DENIED directory must never be recursed into, even when a nested
    subpath re-allows itself via `allowed_read_paths`.

    Config: denied_read_paths=["/work/secret"] + allowed_read_paths=["/work/secret/public"].
    `/work/secret` itself fails the read-guard check (denied), so the whole subtree
    under it — including `/work/secret/public`, which independently passes the
    per-path check via the allowed_read_paths override — must not appear in results.
    The old os.walk-based implementation pruned `dirnames` in place so nothing under
    a denied dir was ever visited; `_iter_files_dfs` must gate recursion the same way
    (not just whether the file itself passes the per-file guard).
    """
    files = {
        "/work/out.txt": "MATCH here\n",
        "/work/secret/public/file.txt": "MATCH here too\n",
    }
    cfg = {
        "launch_root": "/work",
        "read_guard": {
            "enabled": True,
            "allowed_read_roots": ["*"],
            "denied_read_paths": ["/work/secret"],
            "allowed_read_paths": ["/work/secret/public"],
        },
    }
    t = bind_os_backend(
        SearchFiles.__new__(SearchFiles),
        FakeBackend(files),
        config_dict=cfg,
    )

    raw = t(path="/work", regex="MATCH")
    payload = json.loads(raw)

    assert payload["ok"] is True
    files_seen = [m["file"] for m in payload["matches"]]
    assert not any("/secret/" in p or p.endswith("/secret") for p in files_seen), (
        f"read-guard leak: denied subtree visible in matches: {files_seen}"
    )
