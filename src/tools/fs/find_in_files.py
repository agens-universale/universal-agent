"""fs_find_in_files — поиск текста/регулярки внутри файлов (один файл или каталог).

Спека автомата скана:
    docs_md/actual/2026-08-11-fs-find-in-files-SCAN-AUTOMATON-SPEC.ru.md
Hits / context / stop A/B/C — FINAL-SPEC.
Классификатор режимов: utils/find_in_files_scan_mode.py (канон, не дублировать).
"""

from __future__ import annotations

import bisect
import copy
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from backends.protocol import WalkOutputTooLargeError
from config_modules.fs_tools import FsFindInFilesConfig
from utils.find_in_files_scan_mode import (
    Needles,
    PatternMode,
    classify_scan_pattern,
)
from tools.fs.agent_hidden import is_agent_hidden
from utils.file_text import (
    BINARY_SAMPLE_BYTES,
    classify_content_from_sample,
    decode_text_bytes,
)
from utils.fs_size_limits import (
    fs_find_in_files_max_bytes,
)
from utils.fs_search_walk import (
    HARD_EXCLUDE_NESTED_DOT_DIRS,
    effective_exclude_dir_names,
    hard_exclude_dir_names_for_prompt,
    should_skip_nested_dir_name,
)
from utils.glob_to_regex import compile_glob
from utils.tool_report_paths import tool_report_abspath
from tools.simple_tool_base import AbstractSimpleTool
from security.read_guard import (
    enforce_read_guard,
    parse_read_guard_config,
    check_read_path_allowed,
    ReadGuardViolation,
    _resolve_skills_root,
)
from security.path_policy import resolve_tool_context, _extract_config_dict

# affix_or re-open budget is the only per-mode open cap (spec §5 / §6.3).
_MODE_CANDIDATE_LIMIT_ATTR = {
    PatternMode.LIKE: "max_candidates_like",
    PatternMode.OR_SIMPLE: "max_candidates_or_simple",
    PatternMode.AFFIX_OR: "max_candidates_affix_or",
    PatternMode.COMPLEX: "max_candidates_complex",
}


@dataclass
class _ScanShared:
    """Shared hit buffer + counters for content workers."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    matches: List[Dict[str, Any]] = field(default_factory=list)
    stop: bool = False
    stop_reason: Optional[str] = None
    files_opened_regex: int = 0
    content_paths_touched: int = 0
    skipped_binary: int = 0
    skipped_encoding: int = 0
    bytes_read: int = 0
    shortlist_len: int = 0


class FindInFiles(AbstractSimpleTool):
    """Поиск совпадений текста/регулярки внутри файлов файловой системы."""

    max_line_content_chars = 500
    line_content_match_pad = 50
    # Context window budget. Without it one match on a minified (single-line)
    # file re-emits the whole line, and the payload grows as
    # matches × line_length — measured 600 MB from a 2 MB file.
    max_context_chars = 5000

    QUERY_MODE_TEXT = "text"
    QUERY_MODE_RE = "python-re"
    QUERY_MODE_RE_DOTALL = "python-re-dotall"
    # Backward-compatible alias (deprecated name).
    QUERY_MODE_REGEXP = QUERY_MODE_RE
    _QUERY_MODES = frozenset(
        {QUERY_MODE_TEXT, QUERY_MODE_RE, QUERY_MODE_RE_DOTALL}
    )

    _PARAM_SCHEMA: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute file or directory path.",
            },
            "pattern": {
                "type": "string",
                "description": (
                    "What to search for. "
                    "Default query_mode=python-re: Python regular expression. "
                    "query_mode=text: exact substring (metacharacters literal). "
                    "query_mode=python-re-dotall: regex where '.' also matches newline."
                ),
            },
            "query_mode": {
                "type": "string",
                "enum": ["text", "python-re", "python-re-dotall"],
                "default": "python-re",
                "description": (
                    "How to interpret pattern. Default python-re. "
                    "python-re: Python regular expression "
                    "('.' does not match newline; ^/$ are line edges). "
                    "text: exact substring only — metacharacters like "
                    "| ( ) . * are literal; use when you need a fixed string "
                    "that would be special in regex. "
                    "python-re-dotall: same as python-re, but '.' also matches "
                    "newline (only when a match must span lines)."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Maximum matches to return. Omit to use the configured "
                    "default (100 unless the deployment changed it); maximum 300."
                ),
            },
            "case_insensitive": {
                "type": "boolean",
                "description": (
                    "Ignore letter case when matching. Default false (case-sensitive, "
                    "like grep). Set true for case-insensitive search."
                ),
                "default": False,
            },
            "file_pattern": {
                "type": "string",
                "description": (
                    "When path is a directory: only search files whose path relative to "
                    "path matches this glob. Basename-only patterns (no '/') match at any "
                    "depth: '*.py' ≡ '**/*.py'. Patterns with '/' are path-globs "
                    "('src/*.ts' = one level under src/; 'src/**/*.ts' = recursive). "
                    "Ignored when path is a single file."
                ),
            },
            "exclude_dirs": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Extra absolute directories to skip (each directory and everything under it). "
                    "At most 32 entries."
                ),
            },
            "show_excluded_dirs": {
                "type": "boolean",
                "description": (
                    "Include the full list of directories skipped during the walk. "
                    "Default false. Turn on only when you need to verify which dirs "
                    "were not entered (hard excludes, dot-dirs, call exclude_dirs). "
                    "Does not include search_stats."
                ),
                "default": False,
            },
        },
        "required": ["path", "pattern"],
        "additionalProperties": False,
    }

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        skip = hard_exclude_dir_names_for_prompt()
        return {
            "type": "function",
            "name": "fs_find_in_files",
            "description": (
                "Search file contents under an absolute file or directory "
                "(not file names). Required: path, pattern. "
                "pattern is a Python regex by default (query_mode defaults to "
                "python-re). Optional query_mode: python-re (default), text "
                "(exact substring; metacharacters literal), python-re-dotall "
                "(cross-line '.'). "
                "At most limit matches (default 100, max 300); no pagination. "
                "Skips binary/large files; no .gitignore. "
                f"Does not enter nested `.gc`, dirs named {skip}, or nested dot-dirs "
                "(path itself is still searched if it is one of those). "
                "Empty matches ≠ absent if the scan stopped early — check "
                "scan_complete / stop_* / guidance. "
                "search_backend=grep ran inside the sandbox; python read each "
                "candidate file from the host (fallback_reason says why). "
                "stop_reason=output_capped: grep output hit the transport ceiling — "
                "narrow pattern/path/file_pattern, not limit."
            ),
            # Copy: callers (inject_tool_fields) mutate parameters in place.
            "parameters": copy.deepcopy(cls._PARAM_SCHEMA),
            "strict": True,
        }

    @classmethod
    def prompt_description(cls) -> str:
        skip = hard_exclude_dir_names_for_prompt()
        return (
            "Tool name: fs_find_in_files\n"
            "Search file contents under an absolute file or directory (not file names).\n"
            "Required: path, pattern. pattern is Python regex by default "
            "(query_mode defaults to python-re).\n"
            "Optional: query_mode (python-re default; text=exact substring; "
            "python-re-dotall=cross-line '.'), "
            "limit (default 100, max 300; no pagination), "
            "case_insensitive (default false), "
            "file_pattern (directory only; basename-only like '*.py' matches any depth), "
            "exclude_dirs, "
            "show_excluded_dirs (default false; full skipped-dirs list when needed).\n"
            f"Skips nested `.gc`, dirs named {skip}, nested dot-dirs, binary/large files; "
            "no .gitignore. If path itself is a skipped-name dir, it is still searched.\n"
            "Empty matches ≠ proof the text is absent when the scan stops early — "
            "read scan_complete, stop_*, guidance; narrow path/file_pattern/pattern.\n"
            "search_backend=grep ran inside the sandbox; python read each candidate "
            "file from the host (fallback_reason says why). stop_reason=output_capped: "
            "grep output hit the transport ceiling — narrow the query, not limit.\n"
        )

    @enforce_read_guard(path_arg="path")
    def __call__(
        self,
        path: str,
        pattern: str,
        query_mode: Optional[str] = QUERY_MODE_RE,
        # None → fs_tools.find_in_files.default_limit (ключ обещает именно это).
        limit: Optional[int] = None,
        case_insensitive: bool = False,
        file_pattern: Optional[str] = None,
        exclude_dirs: Optional[List[str]] = None,
        show_excluded_dirs: bool = False,
    ) -> str:
        return self._dispatch(
            path,
            pattern,
            query_mode,
            limit,
            case_insensitive,
            file_pattern,
            exclude_dirs,
            show_excluded_dirs,
        )

    def _resolve_config(self) -> Tuple[Any, FsFindInFilesConfig, Dict[str, Any]]:
        rg_context = resolve_tool_context(self)
        cfg_dict = _extract_config_dict(self, getattr(rg_context, "ctx", None), {})
        fif_raw = {}
        if isinstance(cfg_dict, dict):
            fs_raw = cfg_dict.get("fs_tools", {})
            if isinstance(fs_raw, dict):
                fif_raw = fs_raw.get("find_in_files", {}) or {}
        try:
            config = FsFindInFilesConfig.model_validate(
                fif_raw if isinstance(fif_raw, dict) else {}
            )
        except Exception:
            config = FsFindInFilesConfig()
        return rg_context, config, cfg_dict if isinstance(cfg_dict, dict) else {}

    def _resolve_max_bytes(self, cfg_dict: Dict[str, Any]) -> int:
        return fs_find_in_files_max_bytes(cfg_dict)

    def _candidate_limit_for_mode(
        self, mode: PatternMode, config: FsFindInFilesConfig
    ) -> int:
        attr = _MODE_CANDIDATE_LIMIT_ATTR[mode]
        return int(getattr(config, attr))

    def _dispatch(
        self,
        path: str,
        pattern: str,
        query_mode: Optional[str],
        limit: int,
        case_insensitive: bool,
        file_pattern: Optional[str],
        exclude_dirs: Optional[List[str]],
        show_excluded_dirs: bool = False,
    ) -> str:
        if not isinstance(path, str):
            return self._error("invalid_argument", "path")
        if not self._backend.isabs(path):
            return self._error(
                "invalid_argument",
                "path",
                "path must be absolute",
            )
        if not isinstance(pattern, str):
            return self._error("invalid_argument", "pattern")
        if pattern == "":
            # Empty re matches every position → flood of zero-width hits up to limit.
            return self._error(
                "invalid_argument",
                "pattern",
                "pattern must be non-empty",
            )
        # Optional arg: omit / None → python-re (schema default).
        if query_mode is None:
            query_mode = self.QUERY_MODE_RE
        if not isinstance(query_mode, str) or query_mode not in self._QUERY_MODES:
            return self._error(
                "invalid_argument",
                "query_mode",
                "query_mode must be 'text', 'python-re', or 'python-re-dotall'",
            )
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool):
                return self._error("invalid_argument", "limit")
            if limit < 1:
                return self._error(
                    "invalid_argument", "limit", "limit must be >= 1"
                )
        if not isinstance(case_insensitive, bool):
            return self._error("invalid_argument", "case_insensitive")
        if file_pattern is not None and not isinstance(file_pattern, str):
            return self._error("invalid_argument", "file_pattern")
        if exclude_dirs is not None and not isinstance(exclude_dirs, list):
            return self._error("invalid_argument", "exclude_dirs")
        if not isinstance(show_excluded_dirs, bool):
            return self._error("invalid_argument", "show_excluded_dirs")

        # text / python-re: line-oriented '.' ; python-re-dotall: '.' spans newlines.
        flags = re.MULTILINE
        if query_mode == self.QUERY_MODE_RE_DOTALL:
            flags |= re.DOTALL
        if case_insensitive:
            flags |= re.IGNORECASE

        if query_mode == self.QUERY_MODE_TEXT:
            try:
                compiled = re.compile(re.escape(pattern), flags)
            except re.error as e:
                return self._error(
                    "invalid_argument", "pattern", f"Invalid pattern: {e}"
                )
            effective_ci = bool(case_insensitive)
            needles = Needles(
                required_literal=pattern,
                or_group=None,
                effective_ci=effective_ci,
            )
            pattern_mode = PatternMode.LIKE
        else:
            try:
                compiled = re.compile(pattern, flags)
            except re.error as e:
                return self._error(
                    "invalid_argument", "pattern", f"Invalid regex: {e}"
                )
            try:
                scan = classify_scan_pattern(
                    pattern, case_insensitive=case_insensitive
                )
            except re.error as e:
                return self._error(
                    "invalid_argument", "pattern", f"Invalid regex: {e}"
                )
            effective_ci = scan.effective_ci
            needles = scan.needles
            pattern_mode = scan.mode

        rg_context, config, cfg_dict = self._resolve_config()
        max_bytes = self._resolve_max_bytes(cfg_dict)

        limit_requested = limit if limit is not None else config.default_limit
        limit_eff = min(limit_requested, config.max_total_matches)
        limit_capped = limit_requested > config.max_total_matches

        backend = self._backend
        if not backend.exists(path):
            return self._error("not_found")

        if backend.is_file(path):
            return self._search_single_file(
                path,
                compiled,
                pattern,
                query_mode,
                pattern_mode,
                effective_ci,
                limit_requested,
                limit_eff,
                limit_capped,
                needles,
                max_bytes,
                config,
            )
        if backend.is_dir(path):
            return self._search_directory(
                path,
                compiled,
                pattern,
                query_mode,
                pattern_mode,
                effective_ci,
                file_pattern,
                exclude_dirs,
                limit_requested,
                limit_eff,
                limit_capped,
                needles,
                max_bytes,
                config,
                rg_context,
                cfg_dict,
                show_excluded_dirs=show_excluded_dirs,
            )
        return self._error("not_a_directory")

    # ------------------------------------------------------------------
    # prefilter / literal gate
    # ------------------------------------------------------------------
    @staticmethod
    def _literal_in(text: str, piece: str, effective_ci: bool) -> bool:
        hay = text.casefold() if effective_ci else text
        probe = piece.casefold() if effective_ci else piece
        return probe in hay

    @staticmethod
    def _prefilter_allows(needles: Needles, text: str) -> bool:
        """Cheap substring gate before full re. False ⇒ match impossible."""
        if not needles.has_prefilter:
            return True
        hay = text.casefold() if needles.effective_ci else text

        def _has(piece: str) -> bool:
            probe = piece.casefold() if needles.effective_ci else piece
            return probe in hay

        if needles.required_literal is not None:
            # affix_or: the group may be optional (``?``/``*``/``{0,n}``), so
            # soundness rests on the required literal alone — same rule as the
            # full-text funnel in ``_match_decoded_text``.
            return _has(needles.required_literal)
        if needles.or_group is not None:
            return any(_has(b) for b in needles.or_group)
        return True

    # ------------------------------------------------------------------
    # match helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _line_start_offsets(lines: List[str]) -> List[int]:
        starts: List[int] = []
        pos = 0
        for line in lines:
            starts.append(pos)
            pos += len(line)
        return starts

    @staticmethod
    def _offset_to_line(line_starts: List[int], offset: int, text_len: int) -> int:
        if not line_starts:
            return 1
        if offset <= 0:
            return 1
        if offset >= text_len:
            return len(line_starts)
        return bisect.bisect_right(line_starts, offset)

    def _clip_line_content(
        self,
        content: str,
        *,
        match_start: int = 0,
        match_end: int = 0,
    ) -> str:
        """Start-line text for a hit; long lines keep the match visible.

        - ``len <= max_line_content_chars`` → full line.
        - Match fully inside the head window → first N chars + head marker.
        - Otherwise → window around the match (± ``line_content_match_pad``),
          clipped to the same N-char body budget, with left/right ellipsis
          and a total-length marker.
        """
        max_chars = self.max_line_content_chars
        pad = self.line_content_match_pad
        total = len(content)
        if total <= max_chars:
            return content

        ms = max(0, min(total, match_start))
        me = max(ms, min(total, match_end))

        # Prefer a plain head prefix when the whole match is already visible.
        if me <= max_chars:
            return (
                content[:max_chars]
                + f"...[line_truncated: showing first {max_chars} of {total} chars]"
            )

        match_len = me - ms
        if match_len >= max_chars:
            # Oversized match on one line: show head of the match only.
            lo = ms
            hi = ms + max_chars
            body = content[lo:hi]
        else:
            # Fixed ±pad around match (not a second 500-char dump).
            left_budget = min(pad, ms)
            right_budget = min(pad, total - me)
            # If match+2*pad still exceeds max_chars, shrink pads symmetrically.
            remain = max_chars - match_len
            if left_budget + right_budget > remain:
                left_budget = min(left_budget, remain // 2)
                right_budget = min(right_budget, remain - left_budget)
            lo = ms - left_budget
            hi = me + right_budget
            body = content[lo:hi]

        parts: List[str] = []
        if lo > 0:
            parts.append("...")
        parts.append(body)
        if hi < total:
            parts.append("...")
        parts.append(f"[line_truncated: total {total} chars]")
        return "".join(parts)

    def _start_line_content(
        self,
        lines: List[str],
        line_starts: List[int],
        start_line: int,
        match_start: int,
        match_end: int,
    ) -> str:
        idx = start_line - 1
        if idx < 0 or idx >= len(lines):
            return ""
        line_text = lines[idx].rstrip("\r\n")
        line_abs = line_starts[idx] if idx < len(line_starts) else 0
        # Intersection of [match_start, match_end) with this line's content
        # (excluding line endings). Multiline → from match start to EOL.
        n = len(line_text)
        ms = max(0, min(n, match_start - line_abs))
        me = max(ms, min(n, match_end - line_abs))
        return self._clip_line_content(line_text, match_start=ms, match_end=me)

    def _clip_context_line(self, line: str) -> str:
        """Bound one context line the way ``line_content`` is bounded."""
        max_chars = self.max_line_content_chars
        body = line.rstrip("\n")
        eol = line[len(body):] or "\n"
        if len(body) <= max_chars:
            return line
        return f"{body[:max_chars]}...[line_truncated: total {len(body)} chars]{eol}"

    def _join_context(self, chunks: List[str]) -> str:
        """Join context lines under a character budget (§ payload size).

        Per-line clip keeps minified single-line files from turning one match
        into a multi-megabyte payload; the total cap bounds the whole window.
        """
        out: List[str] = []
        used = 0
        for raw in chunks:
            line = self._clip_context_line(raw)
            if used + len(line) > self.max_context_chars:
                out.append(
                    f"...[context_truncated: budget {self.max_context_chars} chars]...\n"
                )
                break
            out.append(line)
            used += len(line)
        return "".join(out)

    def _build_match_context(
        self,
        lines: List[str],
        start_line: int,
        end_line: int,
        context_num: int = 5,
    ) -> str:
        n = len(lines)
        if end_line - start_line <= 2 * context_num:
            ctx_lo = max(0, start_line - 1 - context_num)
            ctx_hi = min(n, end_line + context_num)
            return self._join_context(lines[ctx_lo:ctx_hi])

        head_lo = max(0, start_line - 1 - context_num)
        head_hi = min(n, start_line + context_num)
        tail_lo = max(0, end_line - 1 - context_num)
        tail_hi = min(n, end_line + context_num)
        if tail_lo <= head_hi:
            return self._join_context(lines[head_lo:tail_hi])

        omitted = tail_lo - head_hi
        marker = (
            f"...[context_truncated: match spans lines {start_line}-{end_line}; "
            f"omitted {omitted} lines between context windows]...\n"
        )
        return self._join_context(
            list(lines[head_lo:head_hi]) + [marker] + list(lines[tail_lo:tail_hi])
        )

    def _run_regex(
        self,
        compiled: re.Pattern,
        text: str,
        file_path: str,
        limit_eff: int,
    ) -> List[Dict[str, Any]]:
        """Run compiled regex on decoded text; return up to limit_eff match dicts."""
        lines = text.splitlines(keepends=True)
        line_starts = self._line_start_offsets(lines)
        text_len = len(text)
        results: List[Dict[str, Any]] = []
        report_path = tool_report_abspath(file_path, workspace=self._workspace)

        for match in compiled.finditer(text):
            start = match.start()
            end = match.end()
            start_line = self._offset_to_line(line_starts, start, text_len)
            end_line = (
                start_line
                if end <= start
                else self._offset_to_line(line_starts, end - 1, text_len)
            )
            results.append(
                {
                    "file": report_path,
                    "line": start_line,
                    "end_line": end_line,
                    "line_content": self._start_line_content(
                        lines,
                        line_starts,
                        start_line,
                        start,
                        end,
                    ),
                    "context": self._build_match_context(lines, start_line, end_line),
                }
            )
            if len(results) >= limit_eff:
                break
        return results

    @staticmethod
    def _cap_stop_info(
        limit_capped: bool,
        limit_requested: int,
        limit_eff: int,
        count: int = 0,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Return (stop_reason, guidance) when only limit was capped."""
        if not limit_capped:
            return None, None
        return (
            "requested_limit_capped",
            f"Requested limit {limit_requested} was capped to {limit_eff}; "
            f"scan finished within limit_eff with {count} matches.",
        )

    @staticmethod
    def _output_capped_guidance() -> str:
        return (
            "grep output hit the sandbox transport ceiling before the match limit: "
            "the matches above are a prefix, not the full set. Raising limit cannot "
            "help — narrow pattern, path or file_pattern instead."
        )

    @staticmethod
    def _match_limit_guidance(
        limit_eff: int,
        *,
        limit_capped: bool = False,
        limit_requested: Optional[int] = None,
    ) -> str:
        if limit_capped:
            requested = (
                str(limit_requested) if limit_requested is not None else "the requested value"
            )
            return (
                f"Results truncated (showing up to {limit_eff} matches). "
                f"Requested limit {requested} was capped; increasing "
                "limit further will not help — narrow path/pattern."
            )
        return (
            f"Results truncated (showing up to {limit_eff} matches). "
            "Increase limit if you need more."
        )

    # ------------------------------------------------------------------
    # per-file funnel (§6.2)
    # ------------------------------------------------------------------
    def _open_decode_text(
        self,
        abs_path: str,
        file_size: Optional[int] = None,
    ) -> Tuple[Optional[str], int, Optional[str]]:
        """Head → binary? → body once → decode.

        Returns (text | None, bytes_read, skip_reason) where skip_reason is
        ``binary`` / ``encoding`` / ``read_error`` / None on success.
        """
        backend = self._backend
        try:
            head = backend.read_head_bytes(abs_path, BINARY_SAMPLE_BYTES)
        except OSError:
            return None, 0, "read_error"
        bytes_read = len(head)

        classification = classify_content_from_sample(abs_path, head)
        if classification.is_binary:
            return None, bytes_read, "binary"

        size = file_size
        if size is None:
            try:
                size = backend.getsize(abs_path)
            except OSError:
                size = None

        if size is not None and size <= len(head):
            raw = head
        else:
            try:
                raw = backend.read_bytes(abs_path)
            except OSError:
                return None, bytes_read, "read_error"
            bytes_read = len(raw)

        decoded = decode_text_bytes(raw, encoding=None)
        if not decoded.ok:
            return None, bytes_read, "encoding"
        return decoded.text or "", bytes_read, None

    def _match_decoded_text(
        self,
        *,
        text: str,
        abs_path: str,
        compiled: re.Pattern,
        needles: Needles,
        pattern_mode: PatternMode,
        limit_remaining: int,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Literal gate + match. Returns (matches, ran_full_re)."""
        if limit_remaining <= 0:
            return [], False

        # §6.2 step 4: required_literal gate (like / affix_or)
        if needles.required_literal is not None:
            if not self._literal_in(
                text, needles.required_literal, needles.effective_ci
            ):
                return [], False

        # Optional or_group any-branch (or_simple / affix_or with pure inner)
        if needles.or_group is not None and needles.required_literal is None:
            # or_simple: optional cheap any-branch; still full re if any branch hits
            if not any(
                self._literal_in(text, b, needles.effective_ci)
                for b in needles.or_group
            ):
                return [], False
        elif needles.or_group is not None and needles.required_literal is not None:
            # affix_or with pure OR: required already passed; optional or_group
            # does not skip re (soundness of required alone); still run re.
            pass

        ran_re = True
        # like / text: still use compiled escaped pattern (same as finditer)
        matches = self._run_regex(compiled, text, abs_path, limit_remaining)
        return matches, ran_re

    # ------------------------------------------------------------------
    # single file (§7)
    # ------------------------------------------------------------------
    def _search_single_file(
        self,
        path: str,
        compiled: re.Pattern,
        pattern: str,
        query_mode: str,
        pattern_mode: PatternMode,
        effective_ci: bool,
        limit_requested: int,
        limit_eff: int,
        limit_capped: bool,
        needles: Needles,
        max_bytes: int,
        config: FsFindInFilesConfig,
    ) -> str:
        backend = self._backend
        report_path = tool_report_abspath(path, workspace=self._workspace)
        stop_reason: Optional[str] = None
        guidance: Optional[str] = None

        try:
            size = backend.getsize(path)
        except OSError as e:
            return self._error("stat_failed", message=str(e))

        if size > max_bytes:
            return self._build_response(
                matches=[],
                count=0,
                stop_reason="file_too_large",
                guidance=(
                    "File exceeds size limit; content not read. Use shell rg/grep "
                    "on this file or read it in chunks. Do not assume the pattern "
                    "is absent."
                ),
                scan_complete=False,
                more_matches_may_exist=False,
                limit_requested=limit_requested,
                limit_eff=limit_eff,
                limit_capped=limit_capped,
                path=report_path,
                pattern=pattern,
                query_mode=query_mode,
                mode="file",
                effective_ci=effective_ci,
            )

        text, _bytes_read, skip = self._open_decode_text(path, file_size=size)
        if skip == "read_error":
            return self._error("read_failed", message="Failed to read file")
        if skip in ("binary", "encoding"):
            _sr, _g = self._cap_stop_info(
                limit_capped, limit_requested, limit_eff, count=0
            )
            return self._build_response(
                matches=[],
                count=0,
                stop_reason=_sr,
                guidance=_g,
                scan_complete=True,
                more_matches_may_exist=False,
                limit_requested=limit_requested,
                limit_eff=limit_eff,
                limit_capped=limit_capped,
                path=report_path,
                pattern=pattern,
                query_mode=query_mode,
                mode="file",
                effective_ci=effective_ci,
            )

        assert text is not None

        # literal gate only (no match possible)
        if needles.required_literal is not None and not self._literal_in(
            text, needles.required_literal, needles.effective_ci
        ):
            _sr, _g = self._cap_stop_info(
                limit_capped, limit_requested, limit_eff, count=0
            )
            return self._build_response(
                matches=[],
                count=0,
                stop_reason=_sr,
                guidance=_g,
                scan_complete=True,
                more_matches_may_exist=False,
                limit_requested=limit_requested,
                limit_eff=limit_eff,
                limit_capped=limit_capped,
                path=report_path,
                pattern=pattern,
                query_mode=query_mode,
                mode="file",
                effective_ci=effective_ci,
            )

        matches, _ran_re = self._match_decoded_text(
            text=text,
            abs_path=path,
            compiled=compiled,
            needles=needles,
            pattern_mode=pattern_mode,
            limit_remaining=limit_eff,
        )
        count = len(matches)
        scan_complete = True
        more_matches_may_exist = False

        if count >= limit_eff:
            stop_reason = "match_limit"
            more_matches_may_exist = True
            scan_complete = False
            guidance = self._match_limit_guidance(
                limit_eff, limit_capped=limit_capped, limit_requested=limit_requested
            )
        elif limit_capped:
            stop_reason, guidance = self._cap_stop_info(
                True, limit_requested, limit_eff, count=count
            )

        return self._build_response(
            matches=matches,
            count=count,
            stop_reason=stop_reason,
            guidance=guidance,
            scan_complete=scan_complete,
            more_matches_may_exist=more_matches_may_exist,
            limit_requested=limit_requested,
            limit_eff=limit_eff,
            limit_capped=limit_capped,
            path=report_path,
            pattern=pattern,
            query_mode=query_mode,
            mode="file",
            effective_ci=effective_ci,
            search_backend="python",
            fallback_reason="single_file",
        )

    # ------------------------------------------------------------------
    # file_pattern (basename-only → any depth, like fs_find_files_by_name)
    # ------------------------------------------------------------------
    @staticmethod
    def _build_file_pattern_matcher(file_pattern: str):
        """Match relpath against file_pattern.

        Basename-only globs (no ``/``) also match at any depth via an implicit
        ``**/`` prefix, so ``*.py`` ≡ ``**/*.py``. Patterns that already contain
        ``/`` stay path-globs (``src/*.ts`` = one level under ``src/``).
        """
        patterns = [file_pattern]
        if "/" not in file_pattern and not file_pattern.startswith("**/"):
            patterns.append("**/" + file_pattern)
        compiled = [compile_glob(p) for p in patterns]

        def _glob_match(rel_path: str) -> bool:
            rel_n = rel_path.replace("\\", "/")
            return any(r.match(rel_n) for r in compiled)

        return _glob_match

    # ------------------------------------------------------------------
    # exclude_dirs normalize (§2)
    # ------------------------------------------------------------------
    def _normalize_exclude_dirs(
        self,
        exclude_dirs: Optional[List[str]],
        *,
        start_path: str,
        config: FsFindInFilesConfig,
        backend: Any,
    ) -> Tuple[Optional[List[str]], List[Dict[str, str]], Optional[str]]:
        """Return (accepted_abs_dirs, soft_errors, hard_error_message)."""
        if exclude_dirs is None:
            return [], [], None
        if not isinstance(exclude_dirs, list):
            return None, [], "exclude_dirs must be an array of absolute directory paths"

        soft: List[Dict[str, str]] = []
        accepted: List[str] = []
        seen: set[str] = set()

        for item in exclude_dirs:
            if not isinstance(item, str) or not item.strip():
                continue  # empty drop
            raw = item.strip()
            if not backend.isabs(raw):
                return None, [], f"exclude_dirs entry must be absolute: {raw!r}"
            norm = backend.normpath(raw)
            if not backend.exists(norm):
                soft.append({"path": norm, "reason": "missing"})
                continue
            if backend.is_file(norm):
                return None, [], f"exclude_dirs entry is a file, not a directory: {norm}"
            if not backend.is_dir(norm):
                return None, [], f"exclude_dirs entry is not a directory: {norm}"
            if norm not in seen:
                seen.add(norm)
                accepted.append(norm)

        if len(accepted) > config.max_exclude_dirs:
            return (
                None,
                [],
                f"exclude_dirs exceeds maximum of {config.max_exclude_dirs}",
            )

        start_norm = os.path.normpath(start_path)
        for e in accepted:
            if start_norm == e or self._path_is_under(start_norm, e):
                return (
                    None,
                    [],
                    f"start path is inside exclude_dirs entry: {e}",
                )

        return accepted, soft, None

    @staticmethod
    def _path_is_under(path: str, ancestor: str) -> bool:
        path_n = os.path.normpath(path)
        anc_n = os.path.normpath(ancestor)
        if path_n == anc_n:
            return True
        prefix = anc_n + os.sep
        return path_n.startswith(prefix)

    def _is_excluded_by_call(
        self, dirpath: str, exclude_abs: List[str]
    ) -> bool:
        for e in exclude_abs:
            if self._path_is_under(dirpath, e):
                return True
        return False

    # ------------------------------------------------------------------
    # breakdown (§3.1)
    # ------------------------------------------------------------------
    def _build_breakdown(
        self,
        *,
        start_path: str,
        candidates: List[Tuple[str, int, str, int]],
        config: FsFindInFilesConfig,
    ) -> Dict[str, Any]:
        """candidates: (abs_path, depth, relpath, size). Refuse breakdown."""
        # These paths are handed back to the agent as retry suggestions, so they
        # must be spelled in the backend's syntax, not the host's.
        backend = self._backend
        start_norm = backend.normpath(start_path)
        files_at_root = 0
        dir_counts: Dict[str, int] = {}

        for _abs_path, _depth, relpath, _size in candidates:
            if "/" not in relpath and relpath != ".":
                files_at_root += 1
                continue
            first = relpath.split("/", 1)[0]
            child = backend.normpath(backend.join(start_norm, first))
            dir_counts[child] = dir_counts.get(child, 0) + 1

        ranked = sorted(dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        top_n = config.top_level_dirs_cap
        top = ranked[:top_n]
        top_level_dirs = [
            {
                "path": tool_report_abspath(p, workspace=self._workspace),
                "candidate_count": c,
            }
            for p, c in top
        ]
        out: Dict[str, Any] = {
            "candidate_count": len(candidates),
            "top_level_dirs": top_level_dirs,
            "top_level_dirs_shown": len(top_level_dirs),
            "top_level_dirs_total": len(dir_counts),
        }
        if files_at_root > 0:
            out["files_at_root"] = files_at_root
        return out

    def _refuse_guidance(
        self,
        *,
        pattern_mode: PatternMode,
        n: int,
        limit_n: int,
        breakdown: Dict[str, Any],
    ) -> str:
        parts = [
            f"{n} candidate files exceed limit {limit_n}; content was not searched.",
        ]
        shown = int(breakdown.get("top_level_dirs_shown") or 0)
        total = int(breakdown.get("top_level_dirs_total") or 0)
        if total > shown and shown > 0:
            parts.append(
                f"Showing top {shown} of {total} top-level dirs by candidate count (desc)."
            )

        top: List[Dict[str, Any]] = list(breakdown.get("top_level_dirs") or [])
        # Retry path= : dirs that fit under candidate_limit (likely pass gate).
        fitting = [t for t in top if int(t.get("candidate_count") or 0) <= limit_n]
        if fitting:
            retry = fitting[:2]
        elif top:
            # No fitting dirs in shown list → suggest the smallest shown.
            retry = [top[-1]]
        else:
            retry = []

        if retry:
            if len(retry) == 1:
                t0 = retry[0]
                parts.append(
                    f"You may retry with a narrower path, e.g. path={t0['path']} "
                    f"({t0['candidate_count']} candidates)."
                )
            else:
                t0, t1 = retry[0], retry[1]
                parts.append(
                    f"You may retry with a narrower path, e.g. path={t0['path']} "
                    f"({t0['candidate_count']} candidates) "
                    f"or path={t1['path']} ({t1['candidate_count']})."
                )

        oversized = [
            t for t in top if int(t.get("candidate_count") or 0) > limit_n
        ]
        if not oversized and top and n > 0:
            # optional: single dir ≥ ~50% of total surface
            half = n * 0.5
            heavy = [
                t for t in top if int(t.get("candidate_count") or 0) >= half
            ]
            if heavy:
                oversized = [heavy[0]]
        if oversized:
            t = oversized[0]
            parts.append(
                f'You may also pass exclude_dirs=["{t["path"]}"] '
                f"({t['candidate_count']} candidates) to shrink the surface."
            )

        if pattern_mode is PatternMode.COMPLEX:
            parts.append(
                "You may simplify the pattern or use query_mode=text "
                "if a literal search is enough."
            )
        return " ".join(parts)

    @staticmethod
    def _sort_excluded_dirs(
        excluded_dirs: List[Dict[str, str]],
    ) -> List[Dict[str, str]]:
        return sorted(
            excluded_dirs,
            key=lambda e: (e.get("reason") or "", e.get("path") or ""),
        )

    def _try_native_grep_directory(
        self,
        *,
        path: str,
        pattern: str,
        query_mode: str,
        pattern_mode: PatternMode,
        effective_ci: bool,
        file_pattern: Optional[str],
        accepted_excludes: List[str],
        exclude_soft_errors: List[Dict[str, str]],
        limit_requested: int,
        limit_eff: int,
        limit_capped: bool,
        config: FsFindInFilesConfig,
        report_path: str,
        show_excluded_dirs: bool,
        rg_pass: Callable[[str], bool],
    ) -> Tuple[Optional[str], Optional[str]]:
        """Attempt native grep → (response, None); (None, reason) → Python path.

        The reason is the agent-facing ``fallback_reason``: the Python path reads
        every candidate file from the host, so the switch is never silent.
        """
        from utils.native_fs_search import (
            count_native_candidates,
            hard_exclude_names_for_grep,
            native_grep_refusal,
            run_native_grep,
        )
        from utils.pattern_native_gate import is_grep_native_safe

        # Affix/complex open budgets and multi-hit regex semantics stay on Python.
        if pattern_mode in (PatternMode.AFFIX_OR, PatternMode.COMPLEX):
            return None, "pattern_not_native"

        # find prunes directories itself, so this path has no list of skipped
        # dirs to report. Answer such calls from the Python walk instead of
        # accepting the flag and dropping it.
        if show_excluded_dirs:
            return None, "show_excluded_dirs"

        plan = is_grep_native_safe(
            pattern, query_mode=query_mode, case_insensitive=effective_ci
        )
        exclude_names = hard_exclude_names_for_grep(config.exclude_dir_names)
        exclude_dot = True
        refusal = native_grep_refusal(
            self._backend,
            plan=plan,
            exclude_dir_names=exclude_names,
            call_exclude_dirs=accepted_excludes,
            file_pattern=file_pattern,
            exclude_nested_dot_dirs=exclude_dot,
        )
        if refusal is not None:
            return None, refusal
        assert plan is not None

        # Same candidate ceiling as Python path (scan automaton refuse).
        n = count_native_candidates(
            self._backend,
            path,
            exclude_dir_names=exclude_names,
            exclude_nested_dot_dirs=exclude_dot,
            file_pattern=file_pattern,
            exclude_paths=accepted_excludes,
        )
        if n is None:
            return None, "native_error"
        candidate_limit = self._candidate_limit_for_mode(pattern_mode, config)
        if n > candidate_limit:
            # Fall through to Python so refuse envelope/breakdown stay identical.
            return None, "candidate_limit"

        # Per-hit guard, memoized: one policy evaluation per file, not per hit.
        allowed_cache: Dict[str, bool] = {}

        def _hit_allowed(real_abs: str) -> bool:
            cached = allowed_cache.get(real_abs)
            if cached is None:
                cached = rg_pass(real_abs)
                allowed_cache[real_abs] = cached
            return cached

        result = run_native_grep(
            self._backend,
            path,
            plan,
            exclude_dir_names=exclude_names,
            exclude_nested_dot_dirs=exclude_dot,
            file_pattern=file_pattern,
            max_matches=limit_eff,
            report_path=lambda p: p,
            is_dir_target=True,
            allow_path=_hit_allowed,
            exclude_paths=accepted_excludes,
        )
        if result is None or result.failure:
            return None, (result.failure if result is not None else "native_error")

        shown = result.matches
        for m in shown:
            m["file"] = tool_report_abspath(m["file"], workspace=self._workspace)
            clipped = self._clip_line_content(m["line_content"])
            m["line_content"] = clipped
            m["context"] = clipped if clipped.endswith("\n") else clipped + "\n"

        count = len(shown)
        if result.hit_capture_cap and count < limit_eff:
            # grep output was cut by the transport ceiling, not by the match
            # limit: "increase limit" cannot help, and a host-side rescan of
            # every candidate is exactly the cost this path exists to avoid —
            # report the prefix honestly instead.
            return self._build_response(
                matches=shown,
                count=count,
                stop_reason="output_capped",
                guidance=self._output_capped_guidance(),
                scan_complete=False,
                more_matches_may_exist=True,
                limit_requested=limit_requested,
                limit_eff=limit_eff,
                limit_capped=limit_capped,
                path=report_path,
                pattern=pattern,
                query_mode=query_mode,
                mode="dir",
                effective_ci=effective_ci,
                file_pattern=file_pattern,
                exclude_dir_errors=exclude_soft_errors or None,
                search_backend="grep",
            ), None
        # Parity with Python path: count >= limit_eff → match_limit (A boundary).
        hit_limit = bool(result.truncated) or count >= limit_eff
        scan_complete = not hit_limit
        more = hit_limit

        stop_reason: Optional[str] = None
        guidance: Optional[str] = None
        if hit_limit:
            stop_reason = "match_limit"
            guidance = self._match_limit_guidance(
                limit_eff, limit_capped=limit_capped, limit_requested=limit_requested
            )
        elif limit_capped:
            stop_reason, guidance = self._cap_stop_info(
                True, limit_requested, limit_eff, count=count
            )

        return self._build_response(
            matches=shown,
            count=count,
            stop_reason=stop_reason,
            guidance=guidance,
            scan_complete=scan_complete,
            more_matches_may_exist=more,
            limit_requested=limit_requested,
            limit_eff=limit_eff,
            limit_capped=limit_capped,
            path=report_path,
            pattern=pattern,
            query_mode=query_mode,
            mode="dir",
            effective_ci=effective_ci,
            file_pattern=file_pattern,
            exclude_dir_errors=exclude_soft_errors or None,
            search_backend="grep",
        ), None

    # ------------------------------------------------------------------
    # directory (§3 / §5 / §6)
    # ------------------------------------------------------------------
    def _search_directory(
        self,
        path: str,
        compiled: re.Pattern,
        pattern: str,
        query_mode: str,
        pattern_mode: PatternMode,
        effective_ci: bool,
        file_pattern: Optional[str],
        exclude_dirs: Optional[List[str]],
        limit_requested: int,
        limit_eff: int,
        limit_capped: bool,
        needles: Needles,
        max_bytes: int,
        config: FsFindInFilesConfig,
        rg_context: Any,
        cfg_dict: Dict[str, Any],
        *,
        show_excluded_dirs: bool = False,
    ) -> str:
        backend = self._backend
        report_path = tool_report_abspath(path, workspace=self._workspace)

        accepted_excludes, exclude_soft_errors, excl_err = self._normalize_exclude_dirs(
            exclude_dirs,
            start_path=path,
            config=config,
            backend=backend,
        )
        if excl_err is not None:
            return self._error("invalid_argument", "exclude_dirs", excl_err)
        assert accepted_excludes is not None

        rg_cfg = parse_read_guard_config(cfg_dict)
        rg_root = rg_context.launch_root if rg_cfg.enabled else None
        rg_active = rg_cfg.enabled
        rg_extra = None
        if rg_active:
            _skills_root = _resolve_skills_root(cfg_dict)
            if _skills_root:
                rg_extra = [_skills_root]

        def _rg_pass(p: str) -> bool:
            if is_agent_hidden(os.path.basename(os.path.normpath(p))):
                return False
            if not rg_active:
                return True
            try:
                check_read_path_allowed(
                    p,
                    launch_root=rg_root,
                    cfg=rg_cfg,
                    extra_allowed_paths=rg_extra,
                )
            except (ReadGuardViolation, ValueError):
                return False
            return True

        # --- native grep (gates) before Python pipeline ---
        native_resp, fallback_reason = self._try_native_grep_directory(
            path=path,
            pattern=pattern,
            query_mode=query_mode,
            pattern_mode=pattern_mode,
            effective_ci=effective_ci,
            file_pattern=file_pattern,
            accepted_excludes=accepted_excludes,
            exclude_soft_errors=exclude_soft_errors,
            limit_requested=limit_requested,
            limit_eff=limit_eff,
            limit_capped=limit_capped,
            config=config,
            report_path=report_path,
            show_excluded_dirs=show_excluded_dirs,
            rg_pass=_rg_pass,
        )
        if native_resp is not None:
            return native_resp

        # Hard floor always on; config may only add basenames. Nested dot-dirs
        # stay hard-on even if exclude_dot_dirs is set false in YAML.
        exclude_names = effective_exclude_dir_names(config.exclude_dir_names)
        exclude_dot = HARD_EXCLUDE_NESTED_DOT_DIRS or bool(config.exclude_dot_dirs)
        excluded_dirs: List[Dict[str, str]] = []

        def _should_descend(dirpath: str) -> bool:
            name = os.path.basename(os.path.normpath(dirpath))
            if os.path.normpath(dirpath) == os.path.normpath(path):
                return True
            if is_agent_hidden(name):
                return False
            skip_reason = should_skip_nested_dir_name(
                name,
                exclude_names=exclude_names,
                exclude_dot_dirs=exclude_dot,
            )
            if skip_reason is not None:
                excluded_dirs.append(
                    {
                        "path": tool_report_abspath(
                            dirpath, workspace=self._workspace
                        ),
                        "reason": skip_reason,
                    }
                )
                return False
            if accepted_excludes and self._is_excluded_by_call(
                dirpath, accepted_excludes
            ):
                excluded_dirs.append(
                    {
                        "path": tool_report_abspath(
                            dirpath, workspace=self._workspace
                        ),
                        "reason": "call_exclude_dirs",
                    }
                )
                return False
            if not _rg_pass(dirpath):
                return False
            return True

        glob_matcher = (
            self._build_file_pattern_matcher(file_pattern) if file_pattern else None
        )

        # (abs_path, depth, relpath, size) — size carried so content workers do
        # not stat the same file a second time (one round trip per file).
        candidates: List[Tuple[str, int, str, int]] = []
        files_too_large = 0

        # Walk failures get a domain envelope, like list_files and
        # find_files_by_name. Left to apply_tool's blanket handler they came
        # back as internal_tool_error with "do not retry" guidance — the
        # opposite of what a WalkOutputTooLargeError asks the agent to do.
        try:
            for relpath, is_dir in backend.iter_walk(
                path, should_descend=_should_descend
            ):
                if is_dir:
                    continue
                abs_path = backend.join(path, relpath)

                if glob_matcher is not None and not glob_matcher(relpath):
                    continue
                if not _rg_pass(abs_path):
                    continue
                try:
                    fsize = backend.getsize(abs_path)
                except OSError:
                    continue
                if fsize > max_bytes:
                    # Single-file mode answers file_too_large here; the directory
                    # walk must not swallow it (empty matches ≠ absent).
                    files_too_large += 1
                    continue

                depth = relpath.count("/") if relpath != "." else 0
                candidates.append((abs_path, depth, relpath, fsize))
        except WalkOutputTooLargeError as exc:
            return self._error("walk_too_large", message=str(exc))
        except PermissionError as exc:
            return self._error("permission_denied", message=str(exc))
        except Exception as exc:
            return self._error("unexpected_error", message=str(exc))

        n = len(candidates)
        candidate_limit = self._candidate_limit_for_mode(pattern_mode, config)

        # --- gate before content (§5) ---
        if n > candidate_limit:
            breakdown = self._build_breakdown(
                start_path=path, candidates=candidates, config=config
            )
            guidance = self._refuse_guidance(
                pattern_mode=pattern_mode,
                n=n,
                limit_n=candidate_limit,
                breakdown=breakdown,
            )
            return self._build_response(
                matches=[],
                count=0,
                stop_reason="too_many_candidate_files",
                guidance=guidance,
                scan_complete=False,
                more_matches_may_exist=True,
                limit_requested=limit_requested,
                limit_eff=limit_eff,
                limit_capped=limit_capped,
                path=report_path,
                pattern=pattern,
                query_mode=query_mode,
                mode="dir",
                effective_ci=effective_ci,
                file_pattern=file_pattern,
                exclude_dir_errors=exclude_soft_errors or None,
                fallback_reason=fallback_reason,
                candidate_count=n,
                candidate_limit=candidate_limit,
                breakdown=breakdown,
                excluded_dirs=(
                    self._sort_excluded_dirs(excluded_dirs)
                    if show_excluded_dirs
                    else None
                ),
            )

        # --- sort (§6.1) ---
        needle_strings: List[str] = []
        if needles.required_literal is not None:
            needle_strings.append(needles.required_literal)
        if needles.or_group:
            needle_strings.extend(needles.or_group)

        def _basename_key(item: Tuple[str, int, str, int]) -> Tuple[int, int, str]:
            abs_path, depth, _rel, _size = item
            basename = os.path.basename(abs_path)
            if effective_ci:
                bn = basename.casefold()
                ns = [s.casefold() for s in needle_strings]
            else:
                bn = basename
                ns = needle_strings
            basename_hit = 0 if (ns and any(s in bn for s in ns)) else 1
            return (basename_hit, depth, abs_path)

        candidates.sort(key=_basename_key)

        # --- content workers (§6.4) ---
        workers = 2 if n >= config.content_workers_threshold else 1
        max_re_opens: Optional[int] = None
        if pattern_mode is PatternMode.AFFIX_OR:
            max_re_opens = config.max_open_files_affix_or

        shared = _ScanShared()

        def _process_one(item: Tuple[str, int, str, int]) -> None:
            abs_path, _depth, _rel, fsize = item
            with shared.lock:
                if shared.stop:
                    return
                if len(shared.matches) >= limit_eff:
                    shared.stop = True
                    shared.stop_reason = "match_limit"
                    return
                if max_re_opens is not None and shared.files_opened_regex >= max_re_opens:
                    shared.stop = True
                    shared.stop_reason = "too_many_files_opened"
                    return
                shared.content_paths_touched += 1

            text, br, skip = self._open_decode_text(abs_path, file_size=fsize)
            with shared.lock:
                shared.bytes_read += br
                if skip == "binary":
                    shared.skipped_binary += 1
                    return
                if skip == "encoding":
                    shared.skipped_encoding += 1
                    return
                if skip == "read_error" or text is None:
                    return
                if shared.stop or len(shared.matches) >= limit_eff:
                    shared.stop = True
                    if shared.stop_reason is None:
                        shared.stop_reason = "match_limit"
                    return
                remaining = limit_eff - len(shared.matches)

            # prefilter outside lock (CPU on local text)
            if needles.has_prefilter and not self._prefilter_allows(needles, text):
                return

            with shared.lock:
                if shared.stop or len(shared.matches) >= limit_eff:
                    return
                if max_re_opens is not None and shared.files_opened_regex >= max_re_opens:
                    shared.stop = True
                    shared.stop_reason = "too_many_files_opened"
                    return
                remaining = limit_eff - len(shared.matches)
                shared.shortlist_len += 1

            file_matches, ran_re = self._match_decoded_text(
                text=text,
                abs_path=abs_path,
                compiled=compiled,
                needles=needles,
                pattern_mode=pattern_mode,
                limit_remaining=remaining,
            )

            with shared.lock:
                if ran_re:
                    shared.files_opened_regex += 1
                if file_matches and not shared.stop:
                    room = limit_eff - len(shared.matches)
                    if room > 0:
                        shared.matches.extend(file_matches[:room])
                    if len(shared.matches) >= limit_eff:
                        shared.stop = True
                        shared.stop_reason = "match_limit"
                # No post-regex budget check: the pre-open guards above already
                # fire when the NEXT candidate arrives, which is the only moment
                # that proves a file was actually refused. Checking after the
                # last file turned a complete scan into too_many_files_opened.

        try:
            if workers == 1:
                for item in candidates:
                    if shared.stop:
                        break
                    _process_one(item)
            else:
                # Preserve sort order of submission; early-stop via shared.stop
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(_process_one, item) for item in candidates]
                    for fut in as_completed(futures):
                        # surface unexpected errors
                        exc = fut.exception()
                        if exc is not None:
                            raise exc
                        if shared.stop:
                            # cancel pending is best-effort
                            for f in futures:
                                f.cancel()
                            break
        except PermissionError as exc:
            return self._error("permission_denied", message=str(exc))
        except Exception as exc:
            return self._error("unexpected_error", message=str(exc))

        all_matches = shared.matches[:limit_eff]
        count = len(all_matches)

        stop_reason: Optional[str] = None
        guidance: Optional[str] = None
        scan_complete = True
        more_matches_may_exist = False

        if shared.stop_reason == "match_limit" or count >= limit_eff:
            stop_reason = "match_limit"
            more_matches_may_exist = True
            scan_complete = False
            guidance = self._match_limit_guidance(
                limit_eff, limit_capped=limit_capped, limit_requested=limit_requested
            )
        elif shared.stop_reason == "too_many_files_opened":
            stop_reason = "too_many_files_opened"
            more_matches_may_exist = True
            scan_complete = False
            guidance = (
                "Partial scan (regex open budget). More matches may exist. "
                f"Narrow path (e.g. path={report_path}) or simplify pattern; "
                "shell rg is a fallback."
            )
        elif limit_capped:
            stop_reason, guidance = self._cap_stop_info(
                True, limit_requested, limit_eff, count=count
            )

        if files_too_large and guidance is None:
            guidance = (
                f"{files_too_large} file(s) exceeded the size limit and were not "
                "read. Do not assume the pattern is absent from them; open a "
                "specific file to check it."
            )

        return self._build_response(
            matches=all_matches,
            count=count,
            files_too_large=files_too_large or None,
            stop_reason=stop_reason,
            guidance=guidance,
            scan_complete=scan_complete,
            more_matches_may_exist=more_matches_may_exist,
            limit_requested=limit_requested,
            limit_eff=limit_eff,
            limit_capped=limit_capped,
            path=report_path,
            pattern=pattern,
            query_mode=query_mode,
            mode="dir",
            effective_ci=effective_ci,
            file_pattern=file_pattern,
            exclude_dir_errors=exclude_soft_errors or None,
            excluded_dirs=(
                self._sort_excluded_dirs(excluded_dirs)
                if show_excluded_dirs
                else None
            ),
            search_backend="python",
            fallback_reason=fallback_reason,
        )

    # ------------------------------------------------------------------
    # response builders
    # ------------------------------------------------------------------
    _ERROR_MESSAGES: Dict[str, str] = {
        "invalid_argument": "Invalid argument",
        "not_found": "Path does not exist",
        "not_a_directory": "Path is not a file or directory",
        "stat_failed": "Failed to stat path",
        "read_failed": "Failed to read file",
    }

    def _error(
        self,
        error_type: str,
        field: Optional[str] = None,
        message: Optional[str] = None,
    ) -> str:
        msg = message or self._ERROR_MESSAGES.get(error_type, error_type)
        error_obj: Dict[str, Any] = {"type": error_type}
        if field is not None:
            error_obj["field"] = field
        error_obj["message"] = msg
        payload: Dict[str, Any] = {
            "tool": FindInFiles.registered_tool_name(),
            "ok": False,
            "error": error_obj,
            "matches": [],
            "count": 0,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _build_response(
        self,
        *,
        matches: List[Dict[str, Any]],
        count: int,
        stop_reason: Optional[str],
        guidance: Optional[str],
        scan_complete: bool,
        more_matches_may_exist: bool,
        limit_requested: int,
        limit_eff: int,
        limit_capped: bool,
        path: str,
        pattern: str,
        query_mode: str,
        mode: str,
        effective_ci: bool,
        file_pattern: Optional[str] = None,
        exclude_dir_errors: Optional[List[Dict[str, str]]] = None,
        candidate_count: Optional[int] = None,
        candidate_limit: Optional[int] = None,
        breakdown: Optional[Dict[str, Any]] = None,
        excluded_dirs: Optional[List[Dict[str, str]]] = None,
        search_backend: Optional[str] = None,
        files_too_large: Optional[int] = None,
        fallback_reason: Optional[str] = None,
    ) -> str:
        """Slim agent-facing payload (RESPONSE-CONTRACT). Omit-if-absent."""
        payload: Dict[str, Any] = {
            "tool": FindInFiles.registered_tool_name(),
            "ok": True,
            "path": path,
            "pattern": pattern,
            "query_mode": query_mode,
            "mode": mode,
            "matches": matches,
            "count": count,
            "scan_complete": scan_complete,
            "more_matches_may_exist": more_matches_may_exist,
            "case_insensitive": effective_ci,
        }
        if stop_reason is not None:
            payload["stop_reason"] = stop_reason
        if guidance is not None:
            payload["guidance"] = guidance

        # Limits: only when capped or match_limit (agent needs N).
        if limit_capped:
            payload["limit_capped"] = True
            payload["limit_eff"] = limit_eff
            payload["limit_requested"] = limit_requested
        elif stop_reason == "match_limit":
            payload["limit_eff"] = limit_eff

        if file_pattern:
            payload["file_pattern"] = file_pattern
        if exclude_dir_errors:
            payload["exclude_dir_errors"] = exclude_dir_errors
        # Omit-if-absent: silence here would make "no matches" indistinguishable
        # from "never opened".
        if files_too_large:
            payload["files_too_large"] = files_too_large
        if search_backend is not None:
            payload["search_backend"] = search_backend
        # Why the host-side Python path answered instead of in-sandbox grep.
        if fallback_reason is not None:
            payload["fallback_reason"] = fallback_reason

        # Refuse breakdown only (too_many_candidate_files).
        if stop_reason == "too_many_candidate_files":
            if candidate_count is not None:
                payload["candidate_count"] = candidate_count
            if candidate_limit is not None:
                payload["candidate_limit"] = candidate_limit
            if breakdown is not None:
                if breakdown.get("files_at_root"):
                    payload["files_at_root"] = breakdown["files_at_root"]
                top = breakdown.get("top_level_dirs") or []
                total = int(breakdown.get("top_level_dirs_total") or 0)
                if top or total > 0:
                    payload["top_level_dirs"] = top
                    payload["top_level_dirs_shown"] = len(top)
                    payload["top_level_dirs_total"] = total

        # Opt-in full skip list (show_excluded_dirs); omit empty.
        if excluded_dirs:
            payload["excluded_dirs"] = excluded_dirs

        return json.dumps(payload, ensure_ascii=False)
