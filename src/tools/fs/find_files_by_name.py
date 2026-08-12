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
from utils.file_text import BINARY_SAMPLE_BYTES, classify_content_from_sample, guess_encoding
from utils.glob_to_regex import glob_to_regex
from utils.tool_report_paths import tool_report_abspath

_GLOB_META_RE = re.compile(r"[*?\[\]]")

# Hard safety caps (not configurable in v1).
DEFAULT_MAX_MATCHES = 100
HARD_MAX_MATCHES = 200
DEFAULT_MAX_VISITED = 500_000
# Nested dir basenames never descended into (start path itself is always kept).
DEFAULT_EXCLUDE_DIR_NAMES = frozenset({"venv", "__pycache__", "node_modules", ".git"})


def _compile_glob_fullmatch(pattern: str, *, case_insensitive: bool) -> re.Pattern:
    flags = re.IGNORECASE if case_insensitive else 0
    return re.compile(f"^{glob_to_regex(pattern)}$", flags)


class FindFilesByName(AbstractSimpleTool):
    """Full-tree locate by basename, path glob, or path substring (not file contents)."""

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "fs_find_files_by_name",
            "description": (
                "Locate files and directories by name, glob pattern, or path fragment under "
                "an absolute directory `path`. Walks the whole tree (not depth-limited listing). "
                "Matches names/paths only — does NOT search text inside files. "
                "`query`: plain substring (basename or relative path), or a glob "
                "(e.g. '*.py', '**/list_files.py', 'src/tools/**'). "
                "Skips agent scaffolding (.gc), common junk dirs (node_modules, venv, "
                "__pycache__, .git), and nested dot-directories. "
                "Returns matching paths up to max_matches (default 100, max 200); may set truncated."
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
                            "What to find: file/dir name fragment (e.g. 'list_files'), "
                            "path fragment (e.g. 'tools/fs'), or glob "
                            "(e.g. '*.py', '**/*Config*.yml'). "
                            "Plain text matches if it is a substring of the basename or of the "
                            "relative path from `path`. Globs match the relative path; a glob "
                            "without '/' also matches any depth (as **/pattern)."
                        ),
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": (
                            "Match query without regard to case. Default true. "
                            "Set false for case-sensitive match."
                        ),
                    },
                    "max_matches": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": (
                            "Max matches to return. Default 100, hard max 200. "
                            "Search stops early when reached (truncated=true if more may exist)."
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
        return (
            "Tool name: fs_find_files_by_name\n"
            "Locate files/dirs by name, glob, or path fragment under an absolute directory. "
            "Full-tree walk. Name/path only — does NOT search text inside files. "
            "Skips .gc, node_modules, venv, __pycache__, .git, nested dot-dirs. "
            "Prefer over shell find.\n"
            "Args: path (abs dir), query (required), case_insensitive=true, "
            "max_matches=100 (max 200).\n"
            "Plain query matches basename or rel-path substring; globs match rel-path "
            "(basename-only globs apply at any depth).\n"
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
        """size / binary / encoding for a matched file; never raises."""
        size_bytes: Optional[int] = None
        is_binary: Optional[bool] = None
        encoding_guess: Optional[str] = None
        try:
            size_bytes = int(self._backend.getsize(filepath))
        except OSError:
            size_bytes = None
        try:
            sample = self._backend.read_head_bytes(filepath, BINARY_SAMPLE_BYTES)
            classification = classify_content_from_sample(filepath, sample)
            is_binary = classification.is_binary
            if is_binary:
                encoding_guess = None
            elif classification.encoding_guess is not None:
                encoding_guess = classification.encoding_guess
            else:
                encoding_guess, _conf = guess_encoding(sample)
        except OSError:
            is_binary = None
            encoding_guess = None
        return {
            "size_bytes": size_bytes,
            "is_binary": is_binary,
            "encoding_guess": encoding_guess,
        }

    def _entry(self, filepath: str, is_dir: bool) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "path": tool_report_abspath(filepath, workspace=self._workspace),
            "type": "directory" if is_dir else "file",
            "is_hidden": self._backend.is_hidden(filepath),
        }
        if is_dir:
            entry["size_bytes"] = None
            entry["is_binary"] = None
            entry["encoding_guess"] = None
        else:
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

        def _should_descend(abs_dir: str) -> bool:
            name = os.path.basename(os.path.normpath(abs_dir))
            if os.path.normpath(abs_dir) == root_norm:
                return True
            if is_agent_hidden(name):
                return False
            if name.startswith("."):
                return False
            if name in DEFAULT_EXCLUDE_DIR_NAMES:
                return False
            if not self._read_guard_pass(abs_dir, _rg_cfg, _rg_root, _rg_extra):
                return False
            return True

        try:
            walk_iter = self._backend.iter_walk(path, should_descend=_should_descend)
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
                if not self._read_guard_pass(abs_path, _rg_cfg, _rg_root, _rg_extra):
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

        return _result(
            ok=True,
            matches=matches,
            truncated=truncated,
            walk_stats={
                "visited": visited,
                "stop_reason": stop_reason,
                "max_visited": DEFAULT_MAX_VISITED,
            },
        )
