"""Differential tests: base ListFiles over FakeBackend vs base ListFiles over the real fs.

VERIFICATION METHOD: differential (oracle) testing. The base tool driven by the
real host filesystem is the oracle — the same base tool driven by an in-memory
FakeBackend must produce an IDENTICAL structured envelope (modulo absolute path
roots) for a mirrored directory tree. This pins the harbor-proven DFS-reconstruction
traversal (`_walk_directory`) and flat `backend.listdir` traversal (`_list_directory`)
to the same os.walk-order semantics the old inline os.walk/os.listdir traversal had.

Strategy: build a real temp directory tree for one run; mirror the same tree in
FakeBackend for the other.  Compare structured entries/count/truncated modulo
absolute path roots (strip 'path' from each entry, compare only 'type' and
'is_hidden'; also strip the top-level 'path' field).
"""
import json
import os
from types import SimpleNamespace

import pytest

from tests.harbor_support.fakes import FakeBackend, bind_os_backend, strip_path_fields
from tools.backends.local import LocalFsBackend
from tools.fs.list_files import ListFiles

# ---------------------------------------------------------------------------
# Tree layout used in most tests (linear chain — BFS == DFS, kept for compat)
# ---------------------------------------------------------------------------
#
# <root>/
#   .hidden          (file, hidden)
#   a.py             (file)
#   b.txt            (file)
#   sub/             (dir)
#     c.py           (file)
#     deep/          (dir)
#       d.md         (file)

_FAKE_ROOT = "/work"

_FAKE_FILES = {
    f"{_FAKE_ROOT}/.hidden": "h",
    f"{_FAKE_ROOT}/a.py": "py",
    f"{_FAKE_ROOT}/b.txt": "txt",
    f"{_FAKE_ROOT}/sub/c.py": "subpy",
    f"{_FAKE_ROOT}/sub/deep/d.md": "md",
}

# ---------------------------------------------------------------------------
# Branching tree — exposes BFS vs DFS divergence at depth ≥ 2
# ---------------------------------------------------------------------------
#
# <root>/
#   alpha/           (dir)
#     beta/          (dir)
#       z.md         (file)
#     x.py           (file)
#   gamma/           (dir)
#     y.txt          (file)
#
# os.walk DFS top-down order for full traversal:
#   alpha/ (dir), gamma/ (dir), alpha/beta/ (dir), alpha/x.py (file),
#   alpha/beta/z.md (file), gamma/y.txt (file)
#
# BFS (buggy) order:
#   alpha/ (dir), gamma/ (dir), alpha/beta/ (dir), alpha/x.py (file),
#   gamma/y.txt (file), alpha/beta/z.md (file)     ← slots 4 and 5 flipped!
#
# With max_entries=5 truncation the returned SET of entries differs.

_BRANCHING_ROOT = "/proj"

_BRANCHING_FILES = {
    f"{_BRANCHING_ROOT}/alpha/beta/z.md": "zmd",
    f"{_BRANCHING_ROOT}/alpha/x.py": "xpy",
    f"{_BRANCHING_ROOT}/gamma/y.txt": "ytxt",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base():
    t = ListFiles.__new__(ListFiles)
    return bind_os_backend(t, LocalFsBackend())


def _harbor():
    t = ListFiles.__new__(ListFiles)
    return bind_os_backend(t, FakeBackend(_FAKE_FILES))


def _harbor_branching():
    t = ListFiles.__new__(ListFiles)
    return bind_os_backend(t, FakeBackend(_BRANCHING_FILES))


def _make_real_tree(tmp_path: "pathlib.Path"):
    """Mirror _FAKE_FILES under tmp_path."""
    (tmp_path / ".hidden").write_text("h", encoding="utf-8")
    (tmp_path / "a.py").write_text("py", encoding="utf-8")
    (tmp_path / "b.txt").write_text("txt", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("subpy", encoding="utf-8")
    deep = sub / "deep"
    deep.mkdir()
    (deep / "d.md").write_text("md", encoding="utf-8")
    return tmp_path


def _make_branching_tree(tmp_path: "pathlib.Path"):
    """Mirror _BRANCHING_FILES under tmp_path."""
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    beta = alpha / "beta"
    beta.mkdir()
    (beta / "z.md").write_text("zmd", encoding="utf-8")
    (alpha / "x.py").write_text("xpy", encoding="utf-8")
    gamma = tmp_path / "gamma"
    gamma.mkdir()
    (gamma / "y.txt").write_text("ytxt", encoding="utf-8")
    return tmp_path


def _normalise_entries(entries):
    """Strip absolute 'path', keep only relative basename/subpath, type, is_hidden."""
    normed = []
    for e in entries:
        normed.append({"type": e["type"], "is_hidden": e["is_hidden"]})
    return normed


def _compare_results(base_raw: str, harbor_raw: str, case_id: str):
    """Assert structured result equality modulo absolute paths."""
    b = json.loads(base_raw)
    h = json.loads(harbor_raw)

    # top-level non-path fields must match
    assert b["ok"] == h["ok"], f"{case_id}: ok mismatch"
    assert b["max_depth"] == h["max_depth"], f"{case_id}: max_depth mismatch"
    assert b["max_entries"] == h["max_entries"], f"{case_id}: max_entries mismatch"
    assert b["truncated"] == h["truncated"], f"{case_id}: truncated mismatch"
    assert b["count"] == h["count"], f"{case_id}: count mismatch"
    assert b.get("glob") == h.get("glob"), f"{case_id}: glob mismatch"

    # error shape (type key) must match; messages may differ (different paths)
    b_err, h_err = b.get("error"), h.get("error")
    assert (b_err is None) == (h_err is None), f"{case_id}: error presence mismatch"
    if b_err and h_err:
        assert b_err.get("type") == h_err.get("type"), f"{case_id}: error.type mismatch"

    # entries: same count, same type/is_hidden per slot (order preserved)
    b_entries = b.get("entries", [])
    h_entries = h.get("entries", [])
    assert len(b_entries) == len(h_entries), (
        f"{case_id}: entry count mismatch — base {len(b_entries)} vs harbor {len(h_entries)}\n"
        f"base entries: {[e.get('path','?') for e in b_entries]}\n"
        f"harbor entries: {[e.get('path','?') for e in h_entries]}"
    )
    for i, (be, he) in enumerate(zip(b_entries, h_entries)):
        assert be["type"] == he["type"], (
            f"{case_id}: entry[{i}] type mismatch: {be['type']!r} vs {he['type']!r}"
        )
        assert be["is_hidden"] == he["is_hidden"], (
            f"{case_id}: entry[{i}] is_hidden mismatch: {be['is_hidden']} vs {he['is_hidden']}"
        )


def _compare_results_with_relpath(
    base_raw: str,
    base_root: str,
    harbor_raw: str,
    harbor_root: str,
    case_id: str,
):
    """Assert full structural equality including relative-path ordering.

    Strips the absolute root prefix from each entry's 'path' field so that
    the DFS traversal ORDER can be compared between base (real fs) and harbor
    (FakeBackend).  This catches BFS-vs-DFS divergence even when type/is_hidden
    shapes coincidentally match.
    """
    b = json.loads(base_raw)
    h = json.loads(harbor_raw)

    assert b["ok"] == h["ok"], f"{case_id}: ok mismatch"
    assert b["max_depth"] == h["max_depth"], f"{case_id}: max_depth mismatch"
    assert b["max_entries"] == h["max_entries"], f"{case_id}: max_entries mismatch"
    assert b["truncated"] == h["truncated"], f"{case_id}: truncated mismatch"
    assert b["count"] == h["count"], f"{case_id}: count mismatch"

    b_entries = b.get("entries", [])
    h_entries = h.get("entries", [])
    assert len(b_entries) == len(h_entries), (
        f"{case_id}: entry count mismatch — base {len(b_entries)} vs harbor {len(h_entries)}\n"
        f"base  : {[e.get('path','?') for e in b_entries]}\n"
        f"harbor: {[e.get('path','?') for e in h_entries]}"
    )
    for i, (be, he) in enumerate(zip(b_entries, h_entries)):
        b_rel = be["path"].replace(base_root, "").lstrip("/")
        h_rel = he["path"].replace(harbor_root, "").lstrip("/")
        assert b_rel == h_rel, (
            f"{case_id}: entry[{i}] relative-path mismatch: base {b_rel!r} vs harbor {h_rel!r}\n"
            f"  (DFS expects {b_rel!r} at this position, BFS places {h_rel!r} here)\n"
            f"base  order: {[e['path'].replace(base_root,'').lstrip('/') for e in b_entries]}\n"
            f"harbor order: {[e['path'].replace(harbor_root,'').lstrip('/') for e in h_entries]}"
        )
        assert be["type"] == he["type"], f"{case_id}: entry[{i}] type mismatch"
        assert be["is_hidden"] == he["is_hidden"], f"{case_id}: entry[{i}] is_hidden mismatch"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_flat_dir(tmp_path):
    """max_depth=0 (default): only immediate children, sorted alphabetically."""
    _make_real_tree(tmp_path)
    base_raw = _base()(path=str(tmp_path))
    harbor_raw = _harbor()(path=_FAKE_ROOT)
    _compare_results(base_raw, harbor_raw, "flat_dir")


def test_nested_with_max_depth(tmp_path):
    """max_depth=1: traverses one level deep."""
    _make_real_tree(tmp_path)
    base_raw = _base()(path=str(tmp_path), max_depth=1)
    harbor_raw = _harbor()(path=_FAKE_ROOT, max_depth=1)
    _compare_results(base_raw, harbor_raw, "nested_with_max_depth")


def test_full_depth(tmp_path):
    """max_depth=10: full traversal of the tree."""
    _make_real_tree(tmp_path)
    base_raw = _base()(path=str(tmp_path), max_depth=10)
    harbor_raw = _harbor()(path=_FAKE_ROOT, max_depth=10)
    _compare_results(base_raw, harbor_raw, "full_depth")


def test_glob_filter(tmp_path):
    """Glob filter matches relative paths — only .py files at any depth."""
    _make_real_tree(tmp_path)
    base_raw = _base()(path=str(tmp_path), glob="**/*.py", max_depth=5)
    harbor_raw = _harbor()(path=_FAKE_ROOT, glob="**/*.py", max_depth=5)
    _compare_results(base_raw, harbor_raw, "glob_filter")


def test_max_entries_truncation(tmp_path):
    """max_entries=3 with full traversal triggers truncation."""
    _make_real_tree(tmp_path)
    base_raw = _base()(path=str(tmp_path), max_depth=5, max_entries=3)
    harbor_raw = _harbor()(path=_FAKE_ROOT, max_depth=5, max_entries=3)
    _compare_results(base_raw, harbor_raw, "max_entries_truncation")


# ---------------------------------------------------------------------------
# Branching-tree tests — these expose the BFS vs DFS order bug
# ---------------------------------------------------------------------------

def test_branching_full_depth(tmp_path):
    """Branching tree, full traversal: DFS order must match os.walk exactly.

    For a tree with two sibling dirs (alpha/, gamma/) where alpha/ has a
    grandchild (alpha/beta/), BFS visits gamma/ BEFORE alpha/beta/ but
    os.walk DFS visits alpha/beta/ BEFORE gamma/.  The shim must follow DFS.

    Expected DFS order (6 entries):
      [0] alpha/       (dir)
      [1] gamma/       (dir)
      [2] alpha/beta/  (dir)
      [3] alpha/x.py   (file)
      [4] alpha/beta/z.md  (file)
      [5] gamma/y.txt  (file)

    BFS (buggy) would put gamma/y.txt at [4] and alpha/beta/z.md at [5].
    This test uses _compare_results_with_relpath to catch the slot mismatch.
    """
    _make_branching_tree(tmp_path)
    base_raw = _base()(path=str(tmp_path), max_depth=10)
    harbor_raw = _harbor_branching()(path=_BRANCHING_ROOT, max_depth=10)
    _compare_results_with_relpath(
        base_raw, str(tmp_path),
        harbor_raw, _BRANCHING_ROOT,
        "branching_full_depth",
    )


def test_branching_max_entries_truncation(tmp_path):
    """Branching tree with max_entries=5: truncation must cut at the DFS position.

    Full DFS order has 6 entries:
      alpha/ (dir), gamma/ (dir), alpha/beta/ (dir), alpha/x.py (file),
      alpha/beta/z.md (file), gamma/y.txt (file)
    With max_entries=5, DFS drops gamma/y.txt (last entry).
    BFS (buggy) would drop alpha/beta/z.md instead — a different SET returned.
    This test uses _compare_results_with_relpath to catch both order and set divergence.
    """
    _make_branching_tree(tmp_path)
    base_raw = _base()(path=str(tmp_path), max_depth=10, max_entries=5)
    harbor_raw = _harbor_branching()(path=_BRANCHING_ROOT, max_depth=10, max_entries=5)
    _compare_results_with_relpath(
        base_raw, str(tmp_path),
        harbor_raw, _BRANCHING_ROOT,
        "branching_max_entries_truncation",
    )


def test_not_a_directory(tmp_path):
    """Pointing to a file returns not_a_directory error."""
    f = tmp_path / "a.py"
    f.write_text("x", encoding="utf-8")
    base_raw = _base()(path=str(f))

    # fake-backed: /work/a.py is a file in FakeBackend
    t = bind_os_backend(ListFiles.__new__(ListFiles), FakeBackend({"/work/a.py": "x"}))
    harbor_raw = t(path="/work/a.py")
    _compare_results(base_raw, harbor_raw, "not_a_directory")


def test_not_found(tmp_path):
    """Non-existent path returns not_found error."""
    nonexistent = str(tmp_path / "does_not_exist")
    base_raw = _base()(path=nonexistent)

    t = bind_os_backend(ListFiles.__new__(ListFiles), FakeBackend({}))
    harbor_raw = t(path="/work/does_not_exist")
    _compare_results(base_raw, harbor_raw, "not_found")


# ---------------------------------------------------------------------------
# Read-guard regression: denied dirs must be pruned from recursion, not just
# from the emitted entry.
# ---------------------------------------------------------------------------

def test_walk_does_not_descend_into_denied_dir_via_nested_override():
    """A guard-DENIED directory must never be recursed into, even when a
    nested subpath re-allows itself via `allowed_read_paths`.

    Config: denied_read_paths=["/work/secret"] + allowed_read_paths=["/work/secret/public"].
    `/work/secret` itself fails the read-guard check (denied), so the whole
    subtree under it -- including `/work/secret/public`, which independently
    passes the per-path check via the allowed_read_paths override -- must not
    appear in the results. The old os.walk-based base pruned `dirnames` in
    place so nothing under a denied dir was ever visited; `_dfs` must gate
    recursion the same way (not just whether the entry itself is emitted).
    """
    files = {
        "/work/out.txt": "x",
        "/work/secret/public/file.txt": "y",
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
        ListFiles.__new__(ListFiles),
        FakeBackend(files),
        config_dict=cfg,
    )

    raw = t(path="/work", max_depth=5)
    payload = json.loads(raw)

    assert payload["ok"] is True
    paths = [e["path"] for e in payload["entries"]]
    assert not any("/secret/" in p or p.endswith("/secret") for p in paths), (
        f"read-guard leak: denied subtree visible in entries: {paths}"
    )
