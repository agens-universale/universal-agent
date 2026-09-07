"""fs_find_files_by_name — locate files/dirs by name, glob, or path fragment (full tree)."""

from __future__ import annotations

import functools
import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from gc_core.backends.protocol import WalkOutputTooLargeError
from gc_core.config_modules.fs_tools import (
    FsFindFilesByNameConfig,
    parse_find_files_by_name_config,
)
from gc_core.security.path_policy import resolve_tool_context, _extract_config_dict
from gc_core.security.read_guard import (
    ReadGuardViolation,
    check_read_path_allowed,
    enforce_read_guard,
    parse_read_guard_config,
    _resolve_skills_root,
)
from gc_core.tools.fs.agent_hidden import is_agent_hidden
from gc_core.tools.schema_optional import coalesce_optional_string
from gc_core.loop_detector.constants import READ_PROFILE, loop_warn_on_repeat
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.file_text import BINARY_SAMPLE_BYTES, classify_content_from_sample
from gc_core.utils.fs_search_walk import (
    HARD_EXCLUDE_NESTED_DOT_DIRS,
    effective_exclude_dir_names,
    hard_exclude_dir_names_for_prompt,
    should_skip_nested_dir_name,
)
from gc_core.utils.glob_to_regex import glob_to_regex
from gc_core.utils.tool_report_paths import tool_report_abspath

_GLOB_META_RE = re.compile(r"[*?\[\]]")

def _compile_glob_fullmatch(pattern: str, *, case_insensitive: bool) -> re.Pattern:
    flags = re.IGNORECASE if case_insensitive else 0
    return re.compile(f"^{glob_to_regex(pattern)}$", flags)


@loop_warn_on_repeat(**READ_PROFILE)
class FindFilesByName(AbstractSimpleTool):
    """Full-tree locate by basename, path glob, or path substring (not file contents)."""

    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        skip = hard_exclude_dir_names_for_prompt()
        _cfg = parse_find_files_by_name_config(config_dict)
        _dmm, _hmm = _cfg.default_max_matches, _cfg.hard_max_matches
        return {
            "type": "function",
            "name": "fs_find_files_by_name",
            "description": (
                "Find files and directories by name, path fragment, or glob under an absolute "
                "directory path. Searches the whole tree under path. "
                "Matches names and paths only — does not search file contents. "
                "query is a plain substring (file/dir name or path under path) or a glob "
                "(e.g. '*.py', '**/Config*.yml'). Short plain queries can match many paths. "
                "A query containing * ? [ or ] is read as a glob, so a name with literal "
                "brackets (e.g. 'report[1].pdf') cannot be matched exactly — search a "
                "bracket-free fragment of it instead. "
                f"Does not enter nested directories named `.gc`, {skip}, or any nested "
                "directory whose name starts with '.'. "
                "If path itself is such a directory, it is still searched. "
                "case_insensitive defaults to true. "
                f"At most max_matches results (default {_dmm}, maximum {_hmm}); "
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
                            "matches at any depth. "
                            "The mode is inferred: any of * ? [ ] makes the query a glob. "
                            "So 'config[1].yml' is a character class and matches "
                            "'config1.yml', not the literal name — search 'config' instead."
                        ),
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": (
                            "Ignore letter case when matching. Default true. "
                            "Set false for case-sensitive match. "
                            "Does not require an exact file name: query is still "
                            "a substring or glob."
                        ),
                    },
                    "max_matches": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _hmm,
                        "description": (
                            f"Maximum matches to return. Default {_dmm}, maximum {_hmm}. "
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
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> str:
        skip = hard_exclude_dir_names_for_prompt()
        _cfg = parse_find_files_by_name_config(config_dict)
        _dmm, _hmm = _cfg.default_max_matches, _cfg.hard_max_matches
        return (
            "Tool name: fs_find_files_by_name\n"
            "Find files/directories by name, path fragment, or glob under an absolute directory. "
            "Searches the whole tree. Matches names/paths only — not file contents.\n"
            f"Does not enter nested `.gc`, nested dirs named {skip}, or nested dirs whose "
            "name starts with '.'. If path itself is such a directory, it is still searched.\n"
            "query: plain substring of the name or relative path (short strings match many "
            "paths), or a glob (basename globs like '*.py' match at any depth). "
            "The mode is inferred: any of * ? [ ] makes it a glob, so 'config[1].yml' is a "
            "character class matching 'config1.yml' — for a literal bracketed name search a "
            "bracket-free fragment.\n"
            f"Args: path (required, absolute directory), query (required), "
            f"case_insensitive (default true), max_matches (default {_dmm}, maximum {_hmm}).\n"
            "Response: ok, matches[{path, size_bytes, …}], count, truncated, "
            "walk_stats (visited, stop_reason); guidance only when truncated. "
            "type/is_hidden/is_binary are omitted unless non-default "
            "(directory / true / true).\n"
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
        if truncated:
            stop = (walk_stats or {}).get("stop_reason")
            if stop == "max_visited":
                cap = (walk_stats or {}).get("max_visited", 0)
                payload["guidance"] = (
                    f"Search stopped after visiting {cap} entries (max_visited). "
                    "Narrow path or query; increasing max_matches will not help."
                )
            else:
                payload["guidance"] = (
                    f"Results truncated (showing up to {max_matches} matches). "
                    "Increase max_matches if you need more."
                )
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
    ) -> str:
        return self._result(
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

    def _resolve_config(self) -> FsFindFilesByNameConfig:
        """Load find_files_by_name limits from config (FsToolsConfig.find_files_by_name)."""
        context = resolve_tool_context(self)
        cfg_dict = _extract_config_dict(self, getattr(context, "ctx", None), {})
        return parse_find_files_by_name_config(cfg_dict if isinstance(cfg_dict, dict) else {})

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

    def _probe_file_meta(
        self, filepath: str, size_bytes: Optional[int] = None
    ) -> Dict[str, Any]:
        """size / binary for a matched file; never raises.

        ``size_bytes`` comes with the walk row when the backend knows it
        (find -printf %s / lstat) — no second round trip per match.
        """
        is_binary: Optional[bool] = None
        if size_bytes is None:
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
        meta: Dict[str, Any] = {"size_bytes": size_bytes}
        if is_binary:
            meta["is_binary"] = True
        return meta

    def _entry(
        self, filepath: str, is_dir: bool, size: Optional[int] = None
    ) -> Dict[str, Any]:
        # Default match is a visible text file: omit type/is_hidden/is_binary
        # unless they differ (directory / hidden / binary). size_bytes stays
        # for triage.
        entry: Dict[str, Any] = {
            "path": tool_report_abspath(filepath, workspace=self._workspace),
        }
        if is_dir:
            entry["type"] = "directory"
        if self._backend.is_hidden(filepath):
            entry["is_hidden"] = True
        if not is_dir:
            entry.update(self._probe_file_meta(filepath, size))
        return entry

    @enforce_read_guard(path_arg="path")
    def __call__(
        self,
        path: str,
        query: str,
        case_insensitive: bool = True,
        max_matches: Optional[int] = None,
    ) -> str:
        _ffbn_config = self._resolve_config()
        default_max_matches = int(_ffbn_config.default_max_matches)
        hard_max_matches = int(_ffbn_config.hard_max_matches)
        max_visited = int(_ffbn_config.max_visited)

        query_coalesced = coalesce_optional_string(query)
        if query_coalesced is not None:
            query = query_coalesced
        if max_matches is None:
            max_matches = default_max_matches

        path_for_report = path if isinstance(path, str) else ""
        query_for_report = query if isinstance(query, str) else None
        ci = case_insensitive if isinstance(case_insensitive, bool) else bool(case_insensitive)
        mm = max_matches if isinstance(max_matches, int) else default_max_matches

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
        if not self._backend.isabs(path):
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
        if max_matches > hard_max_matches:
            max_matches = hard_max_matches

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
        # Enumeration via backend.iter_walk only (Harbor: private walk channel
        # ~50M fail-closed; local: os.walk). No tool-exec find dump (60k).

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

        try:
            walk_iter = self._backend.iter_walk_entries(
                path,
                should_descend=_should_descend,
                prune_names=tuple(sorted(exclude_names)),
                prune_dot_dirs=HARD_EXCLUDE_NESTED_DOT_DIRS,
            )
            for entry in walk_iter:
                rel, is_d = entry.rel, entry.is_dir
                base = os.path.basename(rel)
                if is_agent_hidden(base):
                    continue
                visited += 1
                if visited > max_visited:
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
                matches.append(self._entry(abs_path, is_d, size=entry.size))
                if len(matches) >= max_matches:
                    truncated = True
                    stop_reason = "max_matches"
                    break
        except WalkOutputTooLargeError as e:
            return _result(
                ok=False,
                error={"type": "walk_too_large", "message": str(e)},
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
                "max_visited": max_visited,
            },
        )
