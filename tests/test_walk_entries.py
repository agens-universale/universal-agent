"""Обход с размерами и prune на стороне бэкенда.

Размер файла приезжает вместе с обходом (find -printf %s / lstat), чтобы тулы
не делали отдельный getsize на каждый файл; жёсткий пол исключений режется
самим обходом, а не только фильтром на хосте.
"""

import os

from backends.local import LocalFsBackend
from backends.walk_util import (
    WalkEntry,
    iter_walk_entries_from_flat,
    iter_walk_from_flat,
    prune_flat_entries,
)
from tests.harbor_support.fakes import FakeBackend


def test_prune_flat_entries_drops_pruned_dirs_and_their_subtrees():
    entries = [
        WalkEntry("node_modules", True),
        WalkEntry("node_modules/x.js", False, 3),
        WalkEntry(".git", True),
        WalkEntry(".git/HEAD", False, 5),
        WalkEntry("src", True),
        WalkEntry("src/a.py", False, 7),
    ]
    kept = prune_flat_entries(entries, prune_names=("node_modules",), prune_dot_dirs=True)
    assert [e.rel for e in kept] == ["src", "src/a.py"]


def test_prune_flat_entries_without_rules_keeps_everything():
    entries = [WalkEntry(".git", True), WalkEntry(".git/HEAD", False, 5)]
    assert prune_flat_entries(entries, prune_names=(), prune_dot_dirs=False) == entries


def test_iter_walk_entries_keeps_the_flat_walk_order():
    entries = [WalkEntry("b", True), WalkEntry("a.py", False, 1), WalkEntry("b/c.py", False, 2)]
    tuples = [(e.rel, e.is_dir) for e in entries]
    walked = list(iter_walk_entries_from_flat(entries))
    assert [(e.rel, e.is_dir) for e in walked] == list(iter_walk_from_flat(tuples))
    assert [e.size for e in walked] == [None, 1, 2]


def test_local_backend_walk_entries_carry_sizes_and_prune(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(b"12345")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_bytes(b"1")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_bytes(b"1")

    entries = list(
        LocalFsBackend().iter_walk_entries(
            str(tmp_path), prune_names=("node_modules",), prune_dot_dirs=True
        )
    )

    assert [(e.rel, e.is_dir, e.size) for e in entries] == [("src", True, None), ("src/a.py", False, 5)]


def test_local_backend_should_descend_still_applies(tmp_path):
    (tmp_path / "skip").mkdir()
    (tmp_path / "skip" / "x.py").write_bytes(b"1")

    entries = list(
        LocalFsBackend().iter_walk_entries(
            str(tmp_path), should_descend=lambda p: os.path.basename(p) != "skip"
        )
    )

    assert [e.rel for e in entries] == ["skip"]


def test_fake_backend_walk_entries_carry_sizes_and_prune():
    backend = FakeBackend({"/work/a.py": "abc", "/work/node_modules/x.js": "z"})
    entries = backend.walk_entries("/work", prune_names=("node_modules",))
    assert [(e.rel, e.is_dir, e.size) for e in entries] == [("a.py", False, 3)]
