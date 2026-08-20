"""fs_find_files_by_name — locate files/dirs by name, glob, or path fragment (full tree)."""

from __future__ import annotations

import functools
import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from security.path_policy import resolve_tool_context, _extract_config_dict
from security.read_guard import (
    ReadGuardViolation,
    check_read_path_allowed,
    enforce_read_guard,
    parse_read_guard_config,
    _resolve_skills_root,
)
from tools.fs.agent_hidden import is_agent_hidden
from tools.schema_optional import coalesce_optional_string
from tools.simple_tool_base import AbstractSimpleTool
from utils.file_text import BINARY_SAMPLE_BYTES, classify_content_from_sample
from utils.fs_search_walk import (
    HARD_EXCLUDE_NESTED_DOT_DIRS,
    effective_exclude_dir_names,
    hard_exclude_dir_names_for_prompt,
    should_skip_nested_dir_name,
)
from utils.glob_to_regex import glob_to_regex
from utils.tool_report_paths import tool_report_abspath

_GLOB_META_RE = re.compile(r"[*?\[\]]")

# Hard safety caps (not configurable in v1).
DEFAULT_MAX_MATCHES = 100
HARD_MAX_MATCHES = 200
DEFAULT_MAX_VISITED = 500_000


def _compile_glob_fullmatch(pattern: str, *, case_insensitive: bool) -> re.Pattern:
    flags = re.IGNORECASE if case_insensitive else 0
    return re.compile(f"^{glob_to_regex(pattern)}$", flags)


class FindFilesByName(AbstractSimpleTool):
    """Full-tree locate by basename, path glob, or path substring (not file contents)."""

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        skip = hard_exclude_dir_names_for_prompt()
        return {
            "type": "function",
            "name": "fs_find_files_by_name",
            "description": (
                "Find files and directories by name, path fragment, or glob under an absolute "
                "directory path. Searches the whole tree under path. "
                "Matches names and paths only — does not search file contents. "
                "query is a plain substring (file/dir name or path under path) or a glob "
                "(e.g. '*.py', '**/Config*.yml'). Short plain queries can match many paths. "
                f"Does not enter nested directories named `.gc`, {skip}, or any nested "
                "directory whose name starts with '.'. "
                "If path itself is such a directory, it is still searched. "
                "case_insensitive defaults to true. "
                "At most max_matches results (default 100, maximum 200); "
                "truncated=true if the search stopped early."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute directory to search under. Must be an existing directory."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Name or path fragment (e.g. 'list_files', 'tools/fs') or glob "
                            "(e.g. '*.py', '**/*Config*.yml'). "
                            "Plain text matches a substring of the file/dir name or of the "
                            "path relative to path. "
                            "Globs match the path relative to path; a glob without '/' "
                            "matches at any depth."
                        ),
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": (
                            "Ignore letter case when matching. Default true. "
                            "Set false for case-sensitive match."
                        ),
                    },
                    "max_matches": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": (
                            "Maximum matches to return. Default 100, maximum 200. "
                            "truncated=true if more matches may exist."
                        ),
                    },
                },
                "required": ["path", "query"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls) -> str:
        skip = hard_exclude_dir_names_for_prompt()
        return (
            "Tool name: fs_find_files_by_name\n"
            "Find files/directories by name, path fragment, or glob under an absolute directory. "
            "Searches the whole tree. Matches names/paths only — not file contents.\n"
            f"Does not enter nested `.gc`, nested dirs named {skip}, or nested dirs whose "
            "name starts with '.'. If path itself is such a directory, it is still searched.\n"
            "query: plain substring of the name or relative path (short strings match many "
            "paths), or a glob (basename globs like '*.py' match at any depth).\n"
            "Args: path (required, absolute directory), query (required), "
            "case_insensitive (default true), max_matches (default 100, maximum 200).\n"
            "Response: ok, matches[{path, type, …}], count, truncated, "
            "walk_stats (visited, stop_reason).\n"
        )

    def _result(
        self,
        *,
        path: str,
        query: Optional[str],
        case_insensitive: bool,
        max_matches: int,
        ok: bool,
        matches: Optional[List[Dict[str, Any]]] = None,
        truncated: bool = False,
        walk_stats: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "tool": FindFilesByName.registered_tool_name(),
            "ok": ok,
            "path": tool_report_abspath(path, workspace=self._workspace),
            "query": query,
            "case_insensitive": case_insensitive,
            "max_matches": max_matches,
            "truncated": truncated,
            "count": len(matches) if matches is not None else 0,
            "matches": matches if matches is not None else [],
        }
        if walk_stats is not None:
            payload["walk_stats"] = walk_stats
        if error is not None:
            payload["error"] = error
        return json.dumps(payload, ensure_ascii=False)

    def _result_listing(
        self,
        *,
        path: str,
        query: Optional[str],
        case_insensitive: bool,
        max_matches: int,
        ok: bool,
        matches: Optional[List[Dict[str, Any]]] = None,
        truncated: bool = False,
        walk_stats: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
        listing_backend: Optional[str] = None,
    ) -> str:
        raw = self._result(
            path=path,
            query=query,
            case_insensitive=case_insensitive,
            max_matches=max_matches,
            ok=ok,
            matches=matches,
            truncated=truncated,
            walk_stats=walk_stats,
            error=error,
        )
        if listing_backend is None:
            return raw
        payload = json.loads(raw)
        payload["listing_backend"] = listing_backend
        return json.dumps(payload, ensure_ascii=False)

    def _resolve_read_guard(self) -> Tuple[Any, Any, Any]:
        context = resolve_tool_context(self)
        config_dict = _extract_config_dict(self, getattr(context, "ctx", None), {})
        cfg = parse_read_guard_config(config_dict)
        if not cfg.enabled:
            return None, None, None
        skills_root = _resolve_skills_root(config_dict)
        extra_allowed = [skills_root] if skills_root else None
        return cfg, context.launch_root, extra_allowed

    @staticmethod
    def _read_guard_pass(filepath: str, rg_cfg, rg_root, rg_extra=None) -> bool:
        if rg_cfg is None:
            return True
        try:
            check_read_path_allowed(
                filepath,
                launch_root=rg_root,
                cfg=rg_cfg,
                extra_allowed_paths=rg_extra,
            )
        except (ReadGuardViolation, ValueError):
            return False
        return True

    @staticmethod
    def _build_matcher(
        query: str, *, case_insensitive: bool
    ) -> Callable[[str, str], bool]:
        """Return matcher(rel_path, basename) -> bool."""
        q = query.replace("\\", "/")
        if not q:
            return lambda _rel, _base: False

        is_glob = bool(_GLOB_META_RE.search(q))

        if is_glob:
            patterns = [q]
            if "/" not in q and not q.startswith("**/"):
                patterns.append("**/" + q)
            compiled = [
                _compile_glob_fullmatch(p, case_insensitive=case_insensitive)
                for p in patterns
            ]

            def _match_glob(rel: str, _base: str) -> bool:
                rel_n = rel.replace("\\", "/")
                return any(r.match(rel_n) for r in compiled)

            return _match_glob

        if case_insensitive:
            q_cf = q.casefold()

            def _match_sub_ci(rel: str, base: str) -> bool:
                rel_n = rel.replace("\\", "/")
                return q_cf in base.casefold() or q_cf in rel_n.casefold()

            return _match_sub_ci

        def _match_sub(rel: str, base: str) -> bool:
            rel_n = rel.replace("\\", "/")
            return q in base or q in rel_n

        return _match_sub

    def _probe_file_meta(self, filepath: str) -> Dict[str, Any]:
        """size / binary for a matched file; never raises."""
        size_bytes: Optional[int] = None
        is_binary: Optional[bool] = None
        try:
            size_bytes = int(self._backend.getsize(filepath))
        except OSError:
            size_bytes = None
        try:
            sample = self._backend.read_head_bytes(filepath, BINARY_SAMPLE_BYTES)
            classification = classify_content_from_sample(filepath, sample)
            is_binary = classification.is_binary
        except OSError:
            is_binary = None
        return {
            "size_bytes": size_bytes,
            "is_binary": is_binary,
        }

    def _entry(self, filepath: str, is_dir: bool) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "path": tool_report_abspath(filepath, workspace=self._workspace),
            "type": "directory" if is_dir else "file",
            "is_hidden": self._backend.is_hidden(filepath),
        }
        # Directories have no file meta; omit null size/binary fields.
        if not is_dir:
            entry.update(self._probe_file_meta(filepath))
        return entry

    @enforce_read_guard(path_arg="path")
    def __call__(
        self,
        path: str,
        query: str,
        case_insensitive: bool = True,
        max_matches: Optional[int] = None,
    ) -> str:
        query_coalesced = coalesce_optional_string(query)
        if query_coalesced is not None:
            query = query_coalesced
        if max_matches is None:
            max_matches = DEFAULT_MAX_MATCHES

        path_for_report = path if isinstance(path, str) else ""
        query_for_report = query if isinstance(query, str) else None
        ci = case_insensitive if isinstance(case_insensitive, bool) else bool(case_insensitive)
        mm = max_matches if isinstance(max_matches, int) else DEFAULT_MAX_MATCHES

        _rg_cfg, _rg_root, _rg_extra = self._resolve_read_guard()
        _result = functools.partial(
            self._result,
            path=path_for_report,
            query=query_for_report,
            case_insensitive=ci,
            max_matches=mm,
        )

        if not isinstance(path, str):
            return _result(ok=False, error={"type": "invalid_argument", "field": "path"})
        if not os.path.isabs(path):
            return _result(
                ok=False,
                error={
                    "type": "invalid_argument",
                    "field": "path",
                    "message": "path must be absolute",
                },
            )
        if not isinstance(query, str) or not query.strip():
            return _result(
                ok=False,
                error={
                    "type": "invalid_argument",
                    "field": "query",
                    "message": "query must be a non-empty string",
                },
            )
        if not isinstance(max_matches, int) or max_matches < 1:
            return _result(ok=False, error={"type": "invalid_argument", "field": "max_matches"})
        if max_matches > HARD_MAX_MATCHES:
            max_matches = HARD_MAX_MATCHES

        query = query.strip()
        path = self._backend.normpath(path)
        _result = functools.partial(
            self._result,
            path=path,
            query=query,
            case_insensitive=ci,
            max_matches=max_matches,
        )

        if not self._backend.exists(path):
            return _result(ok=False, error={"type": "not_found"})
        if not self._backend.is_dir(path):
            return _result(ok=False, error={"type": "not_a_directory"})

        matcher = self._build_matcher(query, case_insensitive=ci)
        matches: List[Dict[str, Any]] = []
        visited = 0
        truncated = False
        stop_reason = "completed"
        root_norm = os.path.normpath(path)
        listing_backend = "python"

        exclude_names = effective_exclude_dir_names()

        def _should_descend(abs_dir: str) -> bool:
            name = os.path.basename(os.path.normpath(abs_dir))
            if os.path.normpath(abs_dir) == root_norm:
                return True
            if is_agent_hidden(name):
                return False
            if should_skip_nested_dir_name(
                name,
                exclude_names=exclude_names,
                exclude_dot_dirs=HARD_EXCLUDE_NESTED_DOT_DIRS,
            ):
                return False
            if not self._read_guard_pass(abs_dir, _rg_cfg, _rg_root, _rg_extra):
                return False
            return True

        def _rel_skipped(rel: str) -> bool:
            """Skip entries under excluded/dot/agent-hidden path segments (not root)."""
            parts = rel.replace("\\", "/").split("/")
            for i, part in enumerate(parts[:-1] if parts else []):
                if not part:
                    continue
                if is_agent_hidden(part):
                    return True
                if should_skip_nested_dir_name(
                    part,
                    exclude_names=exclude_names,
                    exclude_dot_dirs=HARD_EXCLUDE_NESTED_DOT_DIRS,
                ):
                    return True
            base = parts[-1] if parts else ""
            if is_agent_hidden(base):
                return True
            return False

        try:
            # Native: find dump + Python name filter / excludes (spec §5).
            from utils.native_fs_search import native_find_by_name_rows

            native_rows = native_find_by_name_rows(self._backend, path)
            if native_rows is not None:
                listing_backend = "find"
                for rel, is_d in native_rows:
                    if _rel_skipped(rel):
                        continue
                    base = os.path.basename(rel.replace("\\", "/"))
                    visited += 1
                    if visited > DEFAULT_MAX_VISITED:
                        truncated = True
                        stop_reason = "max_visited"
                        break
                    abs_path = self._backend.join(path, rel)
                    if not self._read_guard_pass(
                        abs_path, _rg_cfg, _rg_root, _rg_extra
                    ):
                        continue
                    if not matcher(rel, base):
                        continue
                    matches.append(self._entry(abs_path, is_d))
                    if len(matches) >= max_matches:
                        truncated = True
                        stop_reason = "max_matches"
                        break
            else:
                walk_iter = self._backend.iter_walk(
                    path, should_descend=_should_descend
                )
                for rel, is_d in walk_iter:
                    base = os.path.basename(rel)
                    if is_agent_hidden(base):
                        continue
                    visited += 1
                    if visited > DEFAULT_MAX_VISITED:
                        truncated = True
                        stop_reason = "max_visited"
                        break

                    abs_path = self._backend.join(path, rel)
                    if not self._read_guard_pass(
                        abs_path, _rg_cfg, _rg_root, _rg_extra
                    ):
                        continue
                    if not matcher(rel, base):
                        continue
                    matches.append(self._entry(abs_path, is_d))
                    if len(matches) >= max_matches:
                        truncated = True
                        stop_reason = "max_matches"
                        break
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

        if stop_reason == "completed":
            truncated = False

        return self._result_listing(
            path=path,
            query=query,
            case_insensitive=ci,
            max_matches=max_matches,
            ok=True,
            matches=matches,
            truncated=truncated,
            walk_stats={
                "visited": visited,
                "stop_reason": stop_reason,
                "max_visited": DEFAULT_MAX_VISITED,
            },
            listing_backend=listing_backend,
        )
