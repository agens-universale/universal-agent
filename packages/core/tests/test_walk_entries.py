"""Обход с размерами и prune на стороне бэкенда.

Размер файла приезжает вместе с обходом (find -printf %s / lstat), чтобы тулы
не делали отдельный getsize на каждый файл; жёсткий пол исключений режется
самим обходом, а не только фильтром на хосте.
"""

import os

from gc_core.backends.local import LocalFsBackend
import pytest

from gc_core.backends.walk_util import (
    WalkEntry,
    iter_walk_entries_from_flat,
    iter_walk_from_flat,
    prune_flat_entries,
    pruned_dir_predicate,
)
from gc_testkit.harbor_fakes import FakeBackend


def test_prune_flat_entries_keeps_pruned_dirs_but_drops_their_subtrees():
    # prune = «не спускаться»: сам каталог остаётся в листинге (find печатает его
    # до -prune), иначе поиск по имени не найдёт node_modules / .github.
    entries = [
        WalkEntry("node_modules", True),
        WalkEntry("node_modules/x.js", False, 3),
        WalkEntry(".git", True),
        WalkEntry(".git/HEAD", False, 5),
        WalkEntry("src", True),
        WalkEntry("src/a.py", False, 7),
        WalkEntry("src/node_modules", False, 1),  # файл с именем-исключением — не каталог
    ]
    kept = prune_flat_entries(entries, prune_names=("node_modules",), prune_dot_dirs=True)
    assert [e.rel for e in kept] == ["node_modules", ".git", "src", "src/a.py", "src/node_modules"]


def test_prune_flat_entries_without_rules_keeps_everything():
    entries = [WalkEntry(".git", True), WalkEntry(".git/HEAD", False, 5)]
    assert prune_flat_entries(entries, prune_names=(), prune_dot_dirs=False) == entries


def test_iter_walk_entries_keeps_the_flat_walk_order():
    entries = [WalkEntry("b", True), WalkEntry("a.py", False, 1), WalkEntry("b/c.py", False, 2)]
    tuples = [(e.rel, e.is_dir) for e in entries]
    walked = list(iter_walk_entries_from_flat(entries))
    assert [(e.rel, e.is_dir) for e in walked] == list(iter_walk_from_flat(tuples))
    assert [e.size for e in walked] == [None, 1, 2]


async def test_local_backend_walk_entries_carry_sizes_and_prune(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(b"12345")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_bytes(b"1")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_bytes(b"1")

    entries = [
        e
        async for e in LocalFsBackend().iter_walk_entries(
            str(tmp_path), prune_names=("node_modules",), prune_dot_dirs=True
        )
    ]

    assert [(e.rel, e.is_dir, e.size) for e in entries] == [
        (".git", True, None),
        ("node_modules", True, None),
        ("src", True, None),
        ("src/a.py", False, 5),
    ]


async def test_local_backend_should_descend_still_applies(tmp_path):
    (tmp_path / "skip").mkdir()
    (tmp_path / "skip" / "x.py").write_bytes(b"1")

    entries = [
        e
        async for e in LocalFsBackend().iter_walk_entries(
            str(tmp_path), should_descend=lambda p: os.path.basename(p) != "skip"
        )
    ]

    assert [e.rel for e in entries] == ["skip"]


async def test_fake_backend_walk_entries_carry_sizes_and_prune():
    backend = FakeBackend({"/work/a.py": "abc", "/work/node_modules/x.js": "z"})
    entries = await backend.walk_entries("/work", prune_names=("node_modules",))
    assert [(e.rel, e.is_dir, e.size) for e in entries] == [("node_modules", True, None), ("a.py", False, 3)]


def test_pruned_dir_predicate_is_the_single_rule():
    pruned = pruned_dir_predicate(("node_modules", ""), prune_dot_dirs=True)
    assert pruned("node_modules") is True
    assert pruned(".git") is True
    assert pruned("src") is False
    assert pruned_dir_predicate((), prune_dot_dirs=False)(".git") is False


async def test_local_backend_iter_walk_is_the_entries_walk_without_sizes(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "c.py").write_bytes(b"12")
    (tmp_path / "a.py").write_bytes(b"1")
    backend = LocalFsBackend()
    walked = [pair async for pair in backend.iter_walk(str(tmp_path))]
    assert walked == [
        (e.rel, e.is_dir)
        async for e in backend.iter_walk_entries(str(tmp_path))
    ]
    assert walked == [("b", True), ("a.py", False), ("b/c.py", False)]


async def test_local_backend_symlink_size_is_unknown_not_the_link_length(tmp_path):
    target = tmp_path / "t.txt"
    target.write_bytes(b"12345")
    try:
        os.symlink(str(target), str(tmp_path / "l.txt"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    entries = {
        e.rel: e async for e in LocalFsBackend().iter_walk_entries(str(tmp_path))
    }
    assert entries["t.txt"].size == 5
    assert entries["l.txt"].size is None  # размер узнает потребитель через getsize (по цели)
