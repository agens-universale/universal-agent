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
from tools.fs.agent_hidden import is_agent_hidden


class ListFiles(AbstractSimpleTool):
    """List files and directories under a path with optional depth and glob filter."""

    @classmethod
    def tool_description(cls):
        return {
            "type": "function",
            "name": "fs_list_files",
            "description": (
                "List files and directories under an absolute directory path. "
                "Default max_depth=0 lists only immediate children; raise max_depth to go deeper. "
                "Optional glob filters paths relative to path (only within max_depth). "
                "Does not search file contents. "
                "Shows normal folders as they are (including node_modules, .git, venv, etc.); "
                "omits the `.gc` directory. "
                "At most max_entries results (default 200); truncated=true if more exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute path of the directory to list. "
                            "Must be an existing directory."
                        ),
                    },
                    "glob": {
                        "type": "string",
                        "description": (
                            "Optional filter on paths relative to path (e.g. '*.py', 'src/*'). "
                            "Only entries within max_depth can match; use max_depth > 0 for nested globs."
                        ),
                    },
                    "max_depth": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "How deep to list. "
                            "0 = only immediate children of path (default). "
                            "Higher values include nested directories."
                        )
                    },
                    "max_entries": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Maximum entries to return (files + directories). "
                            "Default 200. truncated=true if the directory has more."
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
List files and directories under an absolute directory path.
Default max_depth=0 = immediate children only; raise max_depth to list nested entries.
Optional glob filters the listing (only paths within max_depth).
Does not search text inside files.
Lists folders as on disk (node_modules, .git, venv, hidden files, …); omits `.gc`.
At most max_entries results (default 200); truncated=true means the list was cut, not that the folder is empty.

Args: path (required, absolute directory), glob (optional), max_depth (default 0), max_entries (default 200).

Response JSON: ok, path, max_depth, max_entries, truncated, count, entries
[{path, type: file|directory, is_hidden}], optional glob, optional error.

Examples:
{"path": "/home/user/project"}
{"path": "/home/user/project", "max_depth": 2}
{"path": "/home/user/project", "glob": "*.py", "max_depth": 0}
'''
        return description

    def _result(
        self,
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
            "path": tool_report_abspath(path, workspace=self._workspace),
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
        # listing_backend set by caller on success paths via optional kw — see __call__

        return json.dumps(payload, ensure_ascii=False)

    def _result_with_backend(
        self,
        *,
        path: str,
        glob: Optional[str],
        max_depth: Optional[int],
        max_entries: Optional[int],
        ok: bool,
        entries: Optional[List[Dict[str, Any]]] = None,
        truncated: bool = False,
        error: Optional[Dict[str, Any]] = None,
        listing_backend: Optional[str] = None,
    ) -> str:
        raw = self._result(
            path=path,
            glob=glob,
            max_depth=max_depth,
            max_entries=max_entries,
            ok=ok,
            entries=entries,
            truncated=truncated,
            error=error,
        )
        if listing_backend is None:
            return raw
        payload = json.loads(raw)
        payload["listing_backend"] = listing_backend
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
        if not os.path.isabs(path):
            return _result(
                ok=False,
                error={
                    "type": "invalid_argument",
                    "field": "path",
                    "message": "path must be absolute",
                },
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

        # Enumeration always via backend traverse API (listdir / iter_walk).
        # Harbor: walk/listdir use private find channel (~50M, fail-closed) —
        # never tool-exec 60k. Local: os.listdir / os.walk. Name/glob filter is
        # Python on that stream (no separate native find dump).
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

        return self._result_with_backend(
            path=path,
            glob=glob,
            max_depth=effective_max_depth,
            max_entries=max_entries,
            ok=True,
            entries=result,
            truncated=hit_limit,
            listing_backend="python",
        )

    def _entry(self, filepath: str, is_dir: bool) -> Dict[str, Any]:
        return {
            "path": tool_report_abspath(filepath, workspace=self._workspace),
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
        except PermissionError:
            raise PermissionError(f"Permission denied: cannot list contents of '{root_path}'")
        except Exception as e:
            raise RuntimeError(f"Failed to list directory '{root_path}': {str(e)}")

        for name, is_dir in entries:  # backend.listdir already returns sorted (name, is_dir)
            if is_agent_hidden(name):
                continue
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
        """Walk (max_depth>=1) via backend.iter_walk (os.walk on local)."""
        def _should_descend(abs_path: str) -> bool:
            if is_agent_hidden(os.path.basename(abs_path)):
                return False
            if not self._read_guard_pass(abs_path, rg_cfg, rg_root, rg_extra):
                return False
            if max_depth is not None:
                rel = os.path.relpath(abs_path, root_path).replace(os.sep, "/")
                if rel.count("/") >= max_depth:
                    return False
            return True

        try:
            walk_iter = self._backend.iter_walk(root_path, should_descend=_should_descend)
        except FileNotFoundError:
            raise
        except OSError as e:
            raise RuntimeError(f"Failed to walk directory '{root_path}': {e}") from e

        for rel, is_d in walk_iter:
            if is_agent_hidden(os.path.basename(rel)):
                continue
            if file_matcher is not None and not file_matcher(rel):
                continue
            abs_path = self._backend.join(root_path, rel)
            if not self._read_guard_pass(abs_path, rg_cfg, rg_root, rg_extra):
                continue
            result.append(self._entry(abs_path, is_d))
            if max_entries is not None and len(result) >= max_entries:
                return True

        return False
