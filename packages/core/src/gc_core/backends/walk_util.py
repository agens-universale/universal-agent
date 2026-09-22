"""DFS walk helpers for flat ``walk()`` results (Harbor/FakeBackend) and the one prune rule.

``LocalFsBackend`` walks the tree natively (``os.scandir``); these utilities
reconstruct the same visit order from a materialized list.

``WalkEntry`` is the sized form of a walk row: backends that learn the file size
during the traversal (``find -printf %s``, ``DirEntry.stat``) hand it over so
tools do not stat every candidate a second time.

Prune semantics — one rule for every backend, twin of
``find ( -type d -name … ) -printf … -prune``: a pruned directory is still
LISTED, only its subtree is cut. Name search must keep finding ``node_modules``
or ``.github`` themselves; content search never needed what is below them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence


@dataclass(frozen=True)
class WalkEntry:
    rel: str
    is_dir: bool
    size: Optional[int] = None  # bytes for files when known cheaply; None otherwise (symlinks too)


def pruned_dir_predicate(prune_names: Sequence[str], prune_dot_dirs: bool) -> Callable[[str], bool]:
    """The single "is this directory name pruned?" rule shared by every walk."""
    names = frozenset(n for n in prune_names if n)

    def _pruned(name: str) -> bool:
        return name in names or (prune_dot_dirs and name.startswith("."))

    return _pruned


def iter_walk_entries_from_flat(
    entries: Sequence[WalkEntry],
    *,
    should_descend: Optional[Callable[[str], bool]] = None,
    join: Optional[Callable[[str, str], str]] = None,
    root: str = "",
) -> Iterable[WalkEntry]:
    """Top-down DFS, dirs first then files at each level (``fs_list_files`` order)."""
    dir_children: dict = defaultdict(list)
    for entry in entries:
        parts = entry.rel.split("/")
        dir_children["/".join(parts[:-1])].append((parts[-1], entry))
    for level in dir_children.values():
        level.sort(key=lambda item: (not item[1].is_dir, item[0]))

    def _dfs(parent_rel: str) -> Iterable[WalkEntry]:
        subdirs: list[str] = []
        for _name, entry in dir_children.get(parent_rel, []):
            yield entry
            if entry.is_dir and (should_descend is None or should_descend(_abs(join, root, entry.rel))):
                subdirs.append(entry.rel)
        for subdir_rel in subdirs:
            yield from _dfs(subdir_rel)

    yield from _dfs("")


def iter_walk_from_flat(
    entries: list,
    *,
    should_descend: Optional[Callable[[str], bool]] = None,
    join: Optional[Callable[[str, str], str]] = None,
    root: str = "",
) -> Iterable[tuple[str, bool]]:
    """``iter_walk_entries_from_flat`` over plain ``(relpath, is_dir)`` rows."""
    sized = [WalkEntry(rel, is_d) for rel, is_d in entries]
    for entry in iter_walk_entries_from_flat(sized, should_descend=should_descend, join=join, root=root):
        yield entry.rel, entry.is_dir


def prune_flat_entries(
    entries: Sequence[WalkEntry],
    *,
    prune_names: Sequence[str] = (),
    prune_dot_dirs: bool = False,
) -> list[WalkEntry]:
    """Host-side twin of ``find ( -type d -name … ) -printf … -prune``: the pruned
    directory itself stays, everything below it is dropped. Files whose own name
    matches are kept (``-type d``)."""
    if not prune_names and not prune_dot_dirs:
        return list(entries)
    pruned = pruned_dir_predicate(prune_names, prune_dot_dirs)
    return [e for e in entries if not any(pruned(p) for p in e.rel.split("/")[:-1])]


def _dfs_flat(dir_children: dict, parent_rel: str, should_descend, join, root: str) -> Iterable[str]:
    subdirs: list[str] = []
    for name, is_d in dir_children.get(parent_rel, []):
        rel = (parent_rel + "/" + name) if parent_rel else name
        if not is_d:
            yield rel
        elif should_descend is None or should_descend(_abs(join, root, rel)):
            subdirs.append(rel)
    for subdir_rel in subdirs:
        yield from _dfs_flat(dir_children, subdir_rel, should_descend, join, root)


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

    yield from _dfs_flat(dir_children, "", should_descend, join, root)


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
