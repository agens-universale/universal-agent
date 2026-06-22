"""Harbor variant of fs_list_files.

Faithful 1:1 reimplementation of ``ListFiles.__call__`` for the External adapter:
same validation, same max_depth/max_entries/glob/truncated logic, same ``_result``
envelope.  The ONLY difference is physical I/O — directory traversal goes through
HarborBackend (the task container) instead of the host filesystem.

``@enforce_read_guard`` is intentionally dropped: the container is the sandbox.
"""
import functools
import json
import os
from typing import Any, Dict, List, Optional

from tools.fs.list_files import ListFiles
from tools.schema_optional import coalesce_optional_string
from utils.glob_to_regex import compile_glob
from utils.tool_report_paths import tool_report_abspath


class HarborListFiles(ListFiles):
    """Lists files in a Harbor container directory with the same structured output as ListFiles."""

    def __call__(
        self,
        path: str = ".",
        glob: Optional[str] = None,
        max_depth: Optional[int] = None,
        max_entries: Optional[int] = 200,
        **kwargs,
    ) -> str:
        glob = coalesce_optional_string(glob)
        effective_max_depth: int = 0 if max_depth is None else max_depth

        backend = self._ctx.harbor_backend

        _result = functools.partial(
            self._result,
            path=path,
            glob=glob,
            max_depth=effective_max_depth,
            max_entries=max_entries,
        )

        # Input validation — mirrors base exactly
        if not isinstance(path, str):
            return _result(ok=False, error={"type": "invalid_argument", "field": "path"})
        if glob is not None and not isinstance(glob, str):
            return _result(ok=False, error={"type": "invalid_argument", "field": "glob"})
        if max_depth is not None and not isinstance(max_depth, int):
            return _result(ok=False, error={"type": "invalid_argument", "field": "max_depth"})
        if isinstance(max_depth, int) and max_depth < 0:
            return _result(ok=False, error={"type": "invalid_argument", "field": "max_depth"})
        if max_entries is not None and not isinstance(max_entries, int):
            return _result(ok=False, error={"type": "invalid_argument", "field": "max_entries"})
        if isinstance(max_entries, int) and max_entries < 1:
            return _result(ok=False, error={"type": "invalid_argument", "field": "max_entries"})

        # Normalize path (removes trailing slashes etc.)
        path = path.rstrip("/") or "/"

        if not backend.exists(path) and not backend.is_dir(path):
            return _result(ok=False, error={"type": "not_found"})
        if not backend.is_dir(path):
            return _result(ok=False, error={"type": "not_a_directory"})

        # Glob matcher: applied against the relative path from root
        file_matcher = None
        if glob:
            glob_re = compile_glob(glob)

            def _matches(rel_path: str) -> bool:
                rel_path = rel_path.replace(os.sep, "/")
                return bool(glob_re.match(rel_path))

            file_matcher = _matches

        scan_max_entries = (max_entries + 1) if max_entries is not None else None

        result: List[Dict[str, Any]] = []
        hit_limit = False

        if effective_max_depth == 0:
            hit_limit = self._harbor_list_flat(
                backend, path, result, file_matcher=file_matcher, max_entries=scan_max_entries
            )
        else:
            hit_limit = self._harbor_walk(
                backend,
                path,
                result,
                file_matcher=file_matcher,
                max_depth=effective_max_depth,
                max_entries=scan_max_entries,
            )

        if max_entries is not None and len(result) > max_entries:
            result = result[:max_entries]

        return _result(ok=True, entries=result, truncated=hit_limit)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _harbor_is_hidden(self, name: str) -> bool:
        """Check if an entry is hidden (starts with '.')."""
        return name.startswith(".")

    def _harbor_entry(self, abs_path: str, is_dir: bool) -> Dict[str, Any]:
        """Build entry dict from an absolute path and type flag."""
        name = abs_path.rsplit("/", 1)[-1]
        return {
            "path": tool_report_abspath(abs_path),
            "type": "directory" if is_dir else "file",
            "is_hidden": self._harbor_is_hidden(name),
        }

    def _harbor_list_flat(
        self,
        backend,
        root: str,
        result: List[Dict[str, Any]],
        *,
        file_matcher=None,
        max_entries: Optional[int] = None,
    ) -> bool:
        """List immediate children only (max_depth=0).

        The base uses sorted(os.listdir()), which mixes dirs and files
        alphabetically — replicate that ordering here.
        """
        all_entries = backend.walk(root)
        # Keep only immediate children (no '/' in relpath)
        immediate = [(rel, is_d) for rel, is_d in all_entries if "/" not in rel]
        # Sort alphabetically (same as sorted(os.listdir()))
        immediate.sort(key=lambda x: x[0])

        for rel, is_d in immediate:
            if file_matcher is not None and not file_matcher(rel):
                continue
            abs_path = root.rstrip("/") + "/" + rel
            result.append(self._harbor_entry(abs_path, is_d))
            if max_entries is not None and len(result) >= max_entries:
                return True

        return False

    def _harbor_walk(
        self,
        backend,
        root: str,
        result: List[Dict[str, Any]],
        *,
        file_matcher=None,
        max_depth: Optional[int] = None,
        max_entries: Optional[int] = None,
    ) -> bool:
        """Walk directory tree (max_depth>=1), mirroring os.walk top-down DFS ordering.

        os.walk yields (dirpath, sorted_dirs, sorted_files) top-down.  At each
        directory it emits the sorted child dirs first, then sorted child files,
        and THEN recurses into each child dir in order before moving to the next
        sibling directory.  This is depth-first, not breadth-first.

        For a branching tree like:
            alpha/ beta/ z.md  and  alpha/ x.py  and  gamma/ y.txt
        os.walk DFS visits: alpha/, gamma/, alpha/beta/, alpha/x.py,
                            alpha/beta/z.md, gamma/y.txt
        BFS would visit:    alpha/, gamma/, alpha/beta/, alpha/x.py,
                            gamma/y.txt, alpha/beta/z.md  — WRONG at depth≥2.
        """
        all_entries = backend.walk(root)
        base = root.rstrip("/") + "/"

        # Group entries by their parent directory (relpath from root, "" = immediate children).
        from collections import defaultdict
        dir_children: dict = defaultdict(list)

        for rel, is_d in all_entries:
            parts = rel.split("/")
            parent_rel = "/".join(parts[:-1])  # "" for depth-1 entries
            name = parts[-1]
            dir_children[parent_rel].append((name, is_d))

        # Sort each level: dirs first (alphabetically), then files (alphabetically).
        # Mirrors os.walk with dirnames.sort() + filenames.sort() and dirs emitted first.
        for parent_rel in dir_children:
            entries = dir_children[parent_rel]
            dirs = sorted((n, True) for n, d in entries if d)
            files = sorted((n, False) for n, d in entries if not d)
            dir_children[parent_rel] = dirs + files

        def _dfs(parent_rel: str, depth: int) -> bool:
            """Recursively visit parent_rel in DFS order, mirroring os.walk."""
            children = dir_children.get(parent_rel, [])
            prune = max_depth is not None and depth >= max_depth

            # Emit all children of this directory (dirs first, then files)
            subdirs_to_recurse = []
            for name, is_d in children:
                rel = (parent_rel + "/" + name) if parent_rel else name
                abs_path = base + rel

                if file_matcher is None or file_matcher(rel):
                    result.append(self._harbor_entry(abs_path, is_d))
                    if max_entries is not None and len(result) >= max_entries:
                        return True  # hit limit early

                if is_d and not prune:
                    subdirs_to_recurse.append(rel)

            # Recurse into each child dir IN ORDER (DFS: fully expand alpha/ before gamma/)
            for subdir_rel in subdirs_to_recurse:
                if _dfs(subdir_rel, depth + 1):
                    return True  # propagate early exit

            return False

        return _dfs("", 0)
