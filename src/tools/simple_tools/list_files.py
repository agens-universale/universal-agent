import json
import functools
import os
import stat
from typing import List, Dict, Any, Optional
from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool
from utils.glob_to_regex import compile_glob


class ListFiles(AbstractSimpleTool):
    """
    Tool to list files and directories in a given path.
    Supports flat and depth-limited listing.
    """

    @classmethod
    def tool_description(cls):
        return {
            "type": "function",
            "name": "list_files",
            "description": (
                "Lists files and directories within a specified absolute path. Supports flat listing (default) "
                "and deeper traversal limited by `max_depth`. Optionally filters results by a glob pattern matched against the "
                "relative path from the root directory. Returns a JSON string object with: ok, path, "
                "max_depth, max_entries, truncated, count, entries, and optional glob/error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The absolute path of the directory to list contents from. Must be a valid existing directory."
                    },
                    "glob": {
                        "type": "string",
                        "description": (
                            "Optional glob pattern to filter entries by their relative path from `path` "
                            "(e.g., '*.py', '**/*.md', 'src/**'). If omitted, all entries are returned."
                        )
                    },
                    "max_depth": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Max directory depth to traverse. "
                            "0 means only immediate children of `path`. "
                            "If omitted: 0."
                        )
                    },
                    "max_entries": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Max number of returned entries (files + directories). "
                            "Default: 200. If exceeded, response will be truncated."
                        )
                    }
                },
                "required": ["path"],
                "additionalProperties": False
            },
            "strict": True
        }

    @classmethod
    def prompt_description(cls):
        description = '''Tool name: list_files  
Tool description: Lists files and directories in the specified directory with optional depth/size limits.
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "The directory absolute path to list contents for."}, "max_depth": {"type": "integer", "minimum": 0, "description": "Max directory depth to traverse. Default 0 = only immediate children."}, "max_entries": {"type": "integer", "minimum": 1, "description": "Max number of returned entries. Default 200."}, "glob": {"type": "string"}}, "required": ["path"], "additionalProperties": false}

Usage:
- Use this tool to explore directory structure.
- Always provide an **absolute path**.
- Prefer `max_depth` to control how deep the traversal goes.
- Set `max_entries` to limit output size.
- Return format (JSON string):
{
  "tool": "list_files",
  "ok": true|false,
  "path": "...",
  "max_depth": integer,
  "max_entries": integer,
  "truncated": true|false,
  "count": integer,
  "entries": [
    { "path": "...", "type": "file"|"directory", "is_hidden": boolean }
  ],
  "glob": "..." (only when `glob` is provided),
  "error": {"type":"...","message":"..."} (only when `ok=false`)
}

Important Rules:
- **NEVER** use external commands like `ls`, `dir`, `find`, etc. — this tool is safe and cross-platform.
- The `path` must be a valid directory.
- If the directory does not exist or is inaccessible, an error will be raised.
- Hidden files (like `.gitignore`) are included regardless of platform.
- On Windows, uses file attributes to detect hidden files.

Examples:
List top-level contents:
{
  "path": "/home/user/project"
}

List deeper files (example):
{
  "path": "/home/user/project",
  "max_depth": 10
}
'''
        return description

    @staticmethod
    def _result(
        *,
        path: str,
        glob: Optional[str],
        max_depth: Optional[int],
        max_entries: Optional[int],
        ok: bool,
        entries: Optional[List[Dict[str, Any]]] = None,
        truncated: bool = False,
        error: Optional[Dict[str, Any]] = None,
    ) -> str:
        payload = {
            "tool": "list_files",
            "ok": ok,
            "path": path,
            "max_depth": max_depth,
            "max_entries": max_entries,
            "truncated": truncated,
            "count": len(entries) if entries is not None else 0,
        }
        if glob is not None:
            payload["glob"] = glob
        if error is not None:
            payload["error"] = error
        payload["entries"] = entries if entries is not None else []

        return json.dumps(payload, ensure_ascii=False)

    def __call__(
        self,
        path: str,
        glob: Optional[str] = None,
        max_depth: Optional[int] = None,
        max_entries: Optional[int] = 200,
    ) -> str:
        effective_max_depth: int = 0 if max_depth is None else max_depth

        _result = functools.partial(
            self._result,
            path=path,
            glob=glob,
            max_depth=effective_max_depth,
            max_entries=max_entries,
        )

        # Input validation
        if not isinstance(path, str):
            return _result(
                ok=False,
                error={"type": "invalid_argument", "field": "path"},
            )
        if glob is not None and not isinstance(glob, str):
            return _result(
                ok=False,
                error={"type": "invalid_argument", "field": "glob"},
            )
        if max_depth is not None and not isinstance(max_depth, int):
            return _result(
                ok=False,
                error={"type": "invalid_argument", "field": "max_depth"},
            )
        if isinstance(max_depth, int) and max_depth < 0:
            return _result(
                ok=False,
                error={"type": "invalid_argument", "field": "max_depth"},
            )
        if max_entries is not None and not isinstance(max_entries, int):
            return _result(
                ok=False,
                error={"type": "invalid_argument", "field": "max_entries"},
            )
        if isinstance(max_entries, int) and max_entries < 1:
            return _result(
                ok=False,
                error={"type": "invalid_argument", "field": "max_entries"},
            )

        path = os.path.normpath(path)

        if not os.path.exists(path):
            return _result(
                ok=False,
                error={"type": "not_found"},
            )
        if not os.path.isdir(path):
            return _result(
                ok=False,
                error={"type": "not_a_directory"},
            )

        file_matcher = None
        if glob:
            glob_re = compile_glob(glob)

            def _matches(rel_path: str) -> bool:
                rel_path = rel_path.replace(os.sep, "/")
                return bool(glob_re.match(rel_path))

            file_matcher = _matches

        result: List[Dict[str, Any]] = []
        try:
            hit_limit: bool
            # To make `truncated` accurate: scan for one extra entry.
            # If we never reach (max_entries + 1), we didn't actually truncate.
            scan_max_entries = None
            if max_entries is not None:
                scan_max_entries = max_entries + 1

            if effective_max_depth == 0:
                hit_limit = self._list_directory(
                    path, result, file_matcher=file_matcher, max_entries=scan_max_entries
                )
            else:
                hit_limit = self._walk_directory(
                    path,
                    result,
                    file_matcher=file_matcher,
                    max_depth=effective_max_depth,
                    max_entries=scan_max_entries,
                )
        except PermissionError as e:
            return _result(
                ok=False,
                error={"type": "permission_denied", "message": str(e)},
            )
        except Exception as e:
            return _result(
                ok=False,
                error={"type": "unexpected_error", "message": str(e)},
            )

        if max_entries is not None and len(result) > max_entries:
            result = result[:max_entries]

        return _result(ok=True, entries=result, truncated=hit_limit)

    def _is_hidden_unix(self, name: str) -> bool:
        """Check if file is hidden on Unix-like systems (starts with .)."""
        return name.startswith('.')

    def _is_hidden_windows(self, filepath: str) -> bool:
        """Check if file is marked as hidden on Windows via file attributes."""
        try:
            attrs = os.stat(filepath).st_file_attributes  # Windows-specific
            return bool(attrs & stat.FILE_ATTRIBUTE_HIDDEN)
        except AttributeError:
            # Fallback for non-Windows platforms
            basename = os.path.basename(filepath)
            return basename.startswith('.')
        except Exception:
            # If we can't read attributes, assume not hidden
            return False

    def _is_hidden(self, filepath: str) -> bool:
        """Cross-platform check for hidden files."""
        if os.name == 'nt':
            return self._is_hidden_windows(filepath)
        else:
            basename = os.path.basename(filepath)
            return self._is_hidden_unix(basename)

    def _get_entry_info(self, filepath: str) -> Dict[str, Any]:
        """Build metadata for a single file or directory."""
        try:
            st = os.lstat(filepath)  # Follow symlinks? No — use lstat
            is_dir = stat.S_ISDIR(st.st_mode)
            entry_type = "directory" if is_dir else "file"
            is_hidden = self._is_hidden(filepath)
        except OSError:
            # If file becomes inaccessible during listing (rare but possible)
            entry_type = "file"
            is_hidden = self._is_hidden(filepath)

        return {
            "path": filepath.replace(os.sep, '/'),  # Normalize to forward slashes
            "type": entry_type,
            "is_hidden": is_hidden
        }

    def _list_directory(
        self,
        root_path: str,
        result: List[Dict[str, Any]],
        *,
        file_matcher=None,
        max_entries: Optional[int] = None,
    ):
        """List only immediate children of the directory."""
        try:
            names = os.listdir(root_path)
        except PermissionError as e:
            raise PermissionError(f"Permission denied: cannot list contents of '{root_path}'")
        except Exception as e:
            raise RuntimeError(f"Failed to list directory '{root_path}': {str(e)}")

        for name in sorted(names):  # Sort for deterministic output
            filepath = os.path.join(root_path, name)
            if file_matcher is not None:
                rel = os.path.relpath(filepath, root_path)
                if not file_matcher(rel):
                    continue
            entry = self._get_entry_info(filepath)
            result.append(entry)
            if max_entries is not None and len(result) >= max_entries:
                return True

        return False

    def _walk_directory(
        self,
        root_path: str,
        result: List[Dict[str, Any]],
        *,
        file_matcher=None,
        max_depth: Optional[int] = None,
        max_entries: Optional[int] = None,
    ):
        """Recursively walk through directory and list all entries."""
        for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
            rel_dir = os.path.relpath(dirpath, root_path)
            current_depth = 0 if rel_dir == "." else len(rel_dir.split(os.sep))

            # Sort for consistent ordering
            dirnames.sort()
            filenames.sort()

            # If current directory depth reached the limit, we can still list its
            # immediate children, but must not descend further.
            prune_children = max_depth is not None and current_depth >= max_depth

            # Add directories first
            for dirname in dirnames:
                dpath = os.path.join(dirpath, dirname)
                if file_matcher is not None:
                    rel = os.path.relpath(dpath, root_path)
                    if not file_matcher(rel):
                        continue
                result.append(self._get_entry_info(dpath))
                if max_entries is not None and len(result) >= max_entries:
                    return True

            # Then add files
            for filename in filenames:
                fpath = os.path.join(dirpath, filename)
                if file_matcher is not None:
                    rel = os.path.relpath(fpath, root_path)
                    if not file_matcher(rel):
                        continue
                result.append(self._get_entry_info(fpath))
                if max_entries is not None and len(result) >= max_entries:
                    return True

            if prune_children:
                # Stop os.walk recursion into subdirectories beyond current depth.
                dirnames[:] = []

        return False
