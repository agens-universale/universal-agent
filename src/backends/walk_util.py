"""DFS walk helpers for flat ``walk()`` results (Harbor/FakeBackend).

``LocalFsBackend`` uses native ``os.walk`` iterators instead; these utilities
reconstruct the same visit order from a materialized ``[(relpath, is_dir)]`` list.

``WalkEntry`` is the sized form of a walk row: backends that learn the file size
during the traversal (``find -printf %s``, ``lstat``) hand it over so tools do not
stat every candidate a second time.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence


@dataclass(frozen=True)
class WalkEntry:
    rel: str
    is_dir: bool
    size: Optional[int] = None  # bytes for files when known cheaply; None otherwise


def iter_walk_from_flat(
    entries: list,
    *,
    should_descend: Optional[Callable[[str], bool]] = None,
    join: Optional[Callable[[str, str], str]] = None,
    root: str = "",
) -> Iterable[tuple[str, bool]]:
    """Top-down DFS, dirs first then files at each level (``fs_list_files`` order)."""
    dir_children = _group_by_parent(entries)
    for parent_rel in dir_children:
        level = dir_children[parent_rel]
        dirs = sorted((n, True) for n, d in level if d)
        files = sorted((n, False) for n, d in level if not d)
        dir_children[parent_rel] = dirs + files

    def _dfs(parent_rel: str) -> Iterable[tuple[str, bool]]:
        subdirs: list[str] = []
        for name, is_d in dir_children.get(parent_rel, []):
            rel = (parent_rel + "/" + name) if parent_rel else name
            yield rel, is_d
            if is_d and (should_descend is None or should_descend(_abs(join, root, rel))):
                subdirs.append(rel)
        for subdir_rel in subdirs:
            yield from _dfs(subdir_rel)

    yield from _dfs("")


def iter_walk_entries_from_flat(
    entries: Sequence[WalkEntry],
    *,
    should_descend: Optional[Callable[[str], bool]] = None,
    join: Optional[Callable[[str, str], str]] = None,
    root: str = "",
) -> Iterable[WalkEntry]:
    """``iter_walk_from_flat`` over sized entries — same order, sizes preserved."""
    by_rel = {e.rel: e for e in entries}
    flat = [(e.rel, e.is_dir) for e in entries]
    for rel, is_d in iter_walk_from_flat(flat, should_descend=should_descend, join=join, root=root):
        yield by_rel.get(rel) or WalkEntry(rel, is_d)


def prune_flat_entries(
    entries: Sequence[WalkEntry],
    *,
    prune_names: Sequence[str] = (),
    prune_dot_dirs: bool = False,
) -> list[WalkEntry]:
    """Host-side twin of ``find ( -type d -name … ) -prune``: drop pruned dirs and
    everything below them. Files whose own name matches are kept (``-type d``)."""
    if not prune_names and not prune_dot_dirs:
        return list(entries)
    names = frozenset(n for n in prune_names if n)

    def _pruned(name: str) -> bool:
        return name in names or (prune_dot_dirs and name.startswith("."))

    out: list[WalkEntry] = []
    for entry in entries:
        parts = entry.rel.split("/")
        if any(_pruned(p) for p in parts[:-1]):
            continue
        if entry.is_dir and _pruned(parts[-1]):
            continue
        out.append(entry)
    return out


def iter_files_from_flat(
    entries: list,
    *,
    should_descend: Optional[Callable[[str], bool]] = None,
    join: Optional[Callable[[str, str], str]] = None,
    root: str = "",
) -> Iterable[str]:
    """Top-down DFS, files at each level before recursing (``os.walk`` file order)."""
    dir_children = _group_by_parent(entries)
    for parent_rel in dir_children:
        level = dir_children[parent_rel]
        dirs = sorted((n, True) for n, d in level if d)
        files = sorted((n, False) for n, d in level if not d)
        dir_children[parent_rel] = files + dirs

    def _dfs(parent_rel: str) -> Iterable[str]:
        subdirs: list[str] = []
        for name, is_d in dir_children.get(parent_rel, []):
            rel = (parent_rel + "/" + name) if parent_rel else name
            if not is_d:
                yield rel
            elif should_descend is None or should_descend(_abs(join, root, rel)):
                subdirs.append(rel)
        for subdir_rel in subdirs:
            yield from _dfs(subdir_rel)

    yield from _dfs("")


def _group_by_parent(entries: list) -> dict:
    dir_children: dict = defaultdict(list)
    for rel, is_d in entries:
        parts = rel.split("/")
        parent_rel = "/".join(parts[:-1])
        name = parts[-1]
        dir_children[parent_rel].append((name, is_d))
    return dir_children


def _abs(join: Optional[Callable[[str, str], str]], root: str, rel: str) -> str:
    if join is not None and root:
        return join(root, rel)
    return rel
