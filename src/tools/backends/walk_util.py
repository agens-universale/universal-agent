"""DFS walk helpers for flat ``walk()`` results (Harbor/FakeBackend).

``LocalFsBackend`` uses native ``os.walk`` iterators instead; these utilities
reconstruct the same visit order from a materialized ``[(relpath, is_dir)]`` list.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable, Iterable, Optional


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
