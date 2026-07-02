import json
import functools
import os
from typing import List, Dict, Any, Optional
from tools.schema_optional import coalesce_optional_string
from tools.simple_tool_base import AbstractSimpleTool
from utils.glob_to_regex import compile_glob
from utils.tool_report_paths import tool_report_abspath
from security.read_guard import (
    enforce_read_guard,
    parse_read_guard_config,
    check_read_path_allowed,
    ReadGuardViolation,
    _resolve_skills_root,
)
from security.path_policy import resolve_tool_context, _extract_config_dict


class ListFiles(AbstractSimpleTool):
    """
    Tool to list files and directories in a given path.
    Supports flat and depth-limited listing.
    """

    @classmethod
    def tool_description(cls):
        return {
            "type": "function",
            "name": "fs_list_files",
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
                            "Glob pattern to filter entries by their relative path from `path` "
                            "(e.g., '*.py', '**/*.md', 'src/**'). When unset, all entries are returned."
                        ),
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
        description = '''Tool name: fs_list_files  
Tool description: Lists files and directories in the specified directory with optional depth/size limits.
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "The directory absolute path to list contents for."}, "max_depth": {"type": "integer", "minimum": 0, "description": "Max directory depth to traverse. Default 0 = only immediate children."}, "max_entries": {"type": "integer", "minimum": 1, "description": "Max number of returned entries. Default 200."}, "glob": {"type": "string"}}, "required": ["path"], "additionalProperties": false}

Usage:
- Use this tool to explore directory structure.
- Always provide an **absolute path**.
- Prefer `max_depth` to control how deep the traversal goes.
- Set `max_entries` to limit output size.
- Return format (JSON string):
{
  "tool": "fs_list_files",
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
            "tool": ListFiles.registered_tool_name(),
            "ok": ok,
            "path": tool_report_abspath(path),
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

    def _resolve_read_guard(self) -> tuple:
        """
        Возвращает (cfg, launch_root, extra_allowed) для per-entry проверки.
        cfg=None означает что guard не сконфигурирован (legacy / unit-test).
        `extra_allowed` — программные exceptions (например, путь до skills).
        """
        context = resolve_tool_context(self)
        config_dict = _extract_config_dict(self, getattr(context, "ctx", None), {})
        cfg = parse_read_guard_config(config_dict)
        if not cfg.enabled:
            return None, None, None
        skills_root = _resolve_skills_root(config_dict)
        extra_allowed = [skills_root] if skills_root else None
        return cfg, context.launch_root, extra_allowed

    @enforce_read_guard(path_arg="path")
    def __call__(
        self,
        path: str,
        glob: Optional[str] = None,
        max_depth: Optional[int] = None,
        max_entries: Optional[int] = 200,
    ) -> str:
        glob = coalesce_optional_string(glob)
        effective_max_depth: int = 0 if max_depth is None else max_depth
        # Per-entry guard для symlink/escape protection во время listing.
        _rg_cfg, _rg_root, _rg_extra = self._resolve_read_guard()

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

        path = self._backend.normpath(path)

        if not self._backend.exists(path):
            return _result(ok=False, error={"type": "not_found"})
        if not self._backend.is_dir(path):
            return _result(ok=False, error={"type": "not_a_directory"})

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
                    path, result, file_matcher=file_matcher, max_entries=scan_max_entries,
                    rg_cfg=_rg_cfg, rg_root=_rg_root, rg_extra=_rg_extra,
                )
            else:
                hit_limit = self._walk_directory(
                    path,
                    result,
                    file_matcher=file_matcher,
                    max_depth=effective_max_depth,
                    max_entries=scan_max_entries,
                    rg_cfg=_rg_cfg, rg_root=_rg_root, rg_extra=_rg_extra,
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

    def _entry(self, filepath: str, is_dir: bool) -> Dict[str, Any]:
        return {
            "path": tool_report_abspath(filepath),
            "type": "directory" if is_dir else "file",
            "is_hidden": self._backend.is_hidden(filepath),
        }

    @staticmethod
    def _read_guard_pass(filepath: str, rg_cfg, rg_root, rg_extra=None) -> bool:
        """True если entry разрешён read_guard'ом (или guard отключён)."""
        if rg_cfg is None:
            return True
        try:
            check_read_path_allowed(
                filepath,
                launch_root=rg_root,
                cfg=rg_cfg,
                extra_allowed_paths=rg_extra,
            )
        except ReadGuardViolation:
            return False
        except ValueError:
            return False
        return True

    def _list_directory(
        self,
        root_path: str,
        result: List[Dict[str, Any]],
        *,
        file_matcher=None,
        max_entries: Optional[int] = None,
        rg_cfg=None,
        rg_root=None,
        rg_extra=None,
    ):
        """List only immediate children of the directory."""
        try:
            entries = self._backend.listdir(root_path)
        except PermissionError as e:
            raise PermissionError(f"Permission denied: cannot list contents of '{root_path}'")
        except Exception as e:
            raise RuntimeError(f"Failed to list directory '{root_path}': {str(e)}")

        for name, is_dir in entries:  # backend.listdir already returns sorted (name, is_dir)
            filepath = self._backend.join(root_path, name)
            if file_matcher is not None:
                rel = os.path.relpath(filepath, root_path)
                if not file_matcher(rel):
                    continue
            # Per-entry read_guard: пропускаем entries которые резолвятся
            # в denied paths (включая symlink escape наружу launch_root).
            if not self._read_guard_pass(filepath, rg_cfg, rg_root, rg_extra):
                continue
            result.append(self._entry(filepath, is_dir))
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
        rg_cfg=None,
        rg_root=None,
        rg_extra=None,
    ):
        """Walk (max_depth>=1) mirroring os.walk top-down DFS: per level sorted
        dirs first, then sorted files, then recurse depth-first (not BFS)."""
        all_entries = self._backend.walk(root_path)

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
                abs_path = self._backend.join(root_path, rel)

                if file_matcher is None or file_matcher(rel):
                    # Per-entry read_guard: пропускаем entries которые резолвятся
                    # в denied paths (включая symlink escape наружу launch_root).
                    if self._read_guard_pass(abs_path, rg_cfg, rg_root, rg_extra):
                        result.append(self._entry(abs_path, is_d))
                        if max_entries is not None and len(result) >= max_entries:
                            return True  # hit limit early

                if is_d and not prune and self._read_guard_pass(abs_path, rg_cfg, rg_root, rg_extra):
                    subdirs_to_recurse.append(rel)

            # Recurse into each child dir IN ORDER (DFS: fully expand alpha/ before gamma/)
            for subdir_rel in subdirs_to_recurse:
                if _dfs(subdir_rel, depth + 1):
                    return True  # propagate early exit

            return False

        return _dfs("", 0)
