"""Harbor variant of fs_search_files.

Faithful 1:1 reimplementation of ``SearchFiles.__call__`` for the External adapter:
same validation, same per-line Python ``re`` matching, same truncation caps, same
pagination, same ``_result`` envelope.  The ONLY differences:
- Directory existence/walk go through ``HarborBackend.walk`` (container).
- File reading goes through ``backend.read`` (container).
- No ``@enforce_read_guard`` — the container is the sandbox.
"""
import functools
import os
import re
from typing import Optional

from tools.fs.search_files import SearchFiles
from tools.schema_optional import coalesce_optional_string
from utils.glob_to_regex import compile_glob
from utils.tool_report_paths import tool_report_abspath


class HarborSearchFiles(SearchFiles):
    """Shim that routes directory walk + file reads through HarborBackend."""

    def __call__(
        self,
        path: str,
        regex: str = "",
        file_pattern: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        **kwargs,
    ) -> str:
        backend = self._ctx.harbor_backend
        file_pattern = coalesce_optional_string(file_pattern)
        _result = functools.partial(
            self._result, path=path, regex=regex, file_pattern=file_pattern
        )

        # --- Input validation (mirrors base exactly) ---
        if not isinstance(path, str):
            return _result(ok=False, error={"type": "invalid_argument", "field": "path"})
        if not isinstance(regex, str):
            return _result(ok=False, error={"type": "invalid_argument", "field": "regex"})
        if file_pattern is not None and not isinstance(file_pattern, str):
            return _result(ok=False, error={"type": "invalid_argument", "field": "file_pattern"})
        if offset is not None and not isinstance(offset, int):
            return _result(ok=False, error={"type": "invalid_argument", "field": "offset"})
        if limit is not None and not isinstance(limit, int):
            return _result(ok=False, error={"type": "invalid_argument", "field": "limit"})

        # --- Validate regex early so we can return the error before any I/O ---
        try:
            compiled_regex = re.compile(regex)
        except re.error as e:
            return _result(
                ok=False,
                error={"type": "invalid_argument", "field": "regex",
                       "message": f"Invalid regex pattern '{regex}': {e}"},
            )

        # --- Directory checks via backend ---
        if not backend.exists(path) and not backend.is_dir(path):
            return _result(ok=False, error={"type": "not_found"})
        if not backend.is_dir(path):
            return _result(ok=False, error={"type": "not_a_directory"})

        # --- Build glob matcher (same logic as base._create_file_matcher) ---
        if file_pattern:
            glob_re = compile_glob(file_pattern)

            def _file_matches(relpath: str) -> bool:
                relpath_fwd = relpath.replace(os.sep, "/")
                return bool(glob_re.match(relpath_fwd))
        else:
            def _file_matches(relpath: str) -> bool:  # type: ignore[misc]
                return True

        # --- Walk the container tree ---
        try:
            entries = backend.walk(path)
        except FileNotFoundError:
            return _result(ok=False, error={"type": "not_found"})

        # Reconstruct os.walk DFS top-down order from the flat [(relpath, is_dir)] list
        # returned by backend.walk.  os.walk top-down yields files at each directory
        # BEFORE recursing into subdirs — i.e., for each dir it emits sorted files first,
        # then depth-first recurses into sorted subdirs.  This matches the base SearchFiles
        # which iterates `for filename in files` inside `os.walk(..., followlinks=False)`.
        from collections import defaultdict
        dir_children: dict = defaultdict(list)
        for rel, is_d in entries:
            parts = rel.split("/")
            parent_rel = "/".join(parts[:-1])  # "" for depth-1 entries
            name = parts[-1]
            dir_children[parent_rel].append((name, is_d))

        # Sort each level: files alphabetically, dirs alphabetically (both independently).
        # DFS visits: sorted files at this dir, then recurse into each sorted subdir.
        for parent_rel in dir_children:
            level = dir_children[parent_rel]
            dirs = sorted((n, True) for n, d in level if d)
            files = sorted((n, False) for n, d in level if not d)
            # Store files-first so DFS emits them before recursing into subdirs.
            dir_children[parent_rel] = files + dirs

        # DFS traversal: emit files at this level, then recurse into subdirs — mirrors
        # os.walk top-down which yields filenames before descending into subdirectories.
        def _iter_files_dfs(parent_rel: str):
            dirs_to_recurse = []
            for name, is_d in dir_children.get(parent_rel, []):
                rel = (parent_rel + "/" + name) if parent_rel else name
                if not is_d:
                    yield rel
                else:
                    dirs_to_recurse.append(rel)
            for subdir_rel in dirs_to_recurse:
                yield from _iter_files_dfs(subdir_rel)

        results = []
        base_path = path.rstrip("/")

        for relpath in _iter_files_dfs(""):
            if not _file_matches(relpath):
                continue

            abs_path = base_path + "/" + relpath
            try:
                content = backend.read(abs_path)
            except (FileNotFoundError, OSError):
                continue

            file_matches = self._search_in_content(abs_path, content, compiled_regex)
            results.extend(file_matches)

        total_matches = len(results)
        slice_ok, slice_err, start_0, lim_eff, off_eff = self._resolve_slice(
            total_matches, offset, limit
        )
        if not slice_ok:
            return _result(ok=False, error=slice_err)

        end_0 = min(start_0 + lim_eff, total_matches)
        page = results[start_0:end_0]
        truncated = end_0 < total_matches
        next_offset = end_0 + 1 if truncated else None

        return _result(
            ok=True,
            matches=page,
            error=None,
            total_matches=total_matches,
            offset=off_eff,
            limit=lim_eff,
            truncated=truncated,
            next_offset=next_offset,
        )

    def _search_in_content(self, file_path: str, content: str, compiled_regex) -> list:
        """Identical per-line matching to SearchFiles._search_in_file but reads from str."""
        context_num = 5
        literal_hints = self._extract_literals(compiled_regex.pattern)

        results = []
        buffer = []
        found_data = None
        max_line_length = 1000
        line_context_window = 500

        lines = content.splitlines(keepends=True)
        for line_num, line in enumerate(lines, start=1):
            line_cutted = (
                line[:max_line_length]
                + f"...[line_cutted. Show first {max_line_length}, cut last {len(line) - max_line_length}]\n"
                if len(line) > max_line_length
                else line
            )
            buffer.append(line_cutted)
            if found_data is not None:
                found_data["context"] = "".join(buffer)
                results.append(found_data)
                found_data = None

            if literal_hints and not any(hint in line for hint in literal_hints):
                buffer = buffer[-context_num:]
                continue

            res = compiled_regex.search(line)
            if res:
                span = res.span()
                left = max(0, span[0] - line_context_window)
                right = min(len(line), span[1] + line_context_window)
                found_content = line[left:right]
                if left > 0:
                    found_content = f"[...cutted first {left}]" + found_content
                if right < len(line):
                    found_content += f"[cutted last {len(line) - right}...]"
                if left > 0 or right < len(line):
                    found_content += f"(shown from {left} to {right} symbols)"
                else:
                    found_content = found_content.strip()
                found_data = {
                    "file": tool_report_abspath(file_path),
                    "line": line_num,
                    "line_content": found_content,
                }
            buffer = buffer[-context_num:]

        if found_data is not None:
            found_data["context"] = "".join(buffer)
            results.append(found_data)

        return results
