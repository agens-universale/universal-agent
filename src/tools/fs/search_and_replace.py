import os
import json
import functools
import difflib
from typing import Dict, Any
from tools.simple_tool_base import AbstractSimpleTool
from utils.tool_report_paths import tool_report_abspath
from security.path_policy import enforce_root_box
from utils.file_text import check_expected_file_sha256, is_valid_sha256_hex, read_text_for_edit


class SearchAndReplace(AbstractSimpleTool):
    """
    Tool: search_and_replace
    Performs a single search and replace operation in a file.
    Only the first occurrence is replaced.
    """
    max_file_size_bytes = 100 * 1024 * 1024
    default_diff_context_lines = 3
    default_max_diff_chars = 12000

    @staticmethod
    def _normalize_spaces(text: str) -> str:
        return " ".join(text.split())

    @classmethod
    def _build_simple_suggestions(
        cls,
        *,
        search: str,
        content: str,
        max_suggestions: int = 10,
        max_lines: int = 5000,
    ) -> tuple[list[Dict[str, Any]], int]:
        suggestions: list[Dict[str, Any]] = []
        lines = content.splitlines()
        limited_lines = lines[:max_lines]
        is_multiline_search = "\n" in search or "\r" in search

        if is_multiline_search:
            search_lines = search.splitlines()
            if not search_lines or len(search_lines) > len(limited_lines):
                return suggestions, 0
            normalized_search = "\n".join(
                cls._normalize_spaces(line).lower().strip()
                for line in search_lines
            )
            if not normalized_search.strip():
                return suggestions, 0

            scored: list[tuple[float, int, str]] = []
            window_size = len(search_lines)
            for start_idx in range(0, len(limited_lines) - window_size + 1):
                window_lines = limited_lines[start_idx:start_idx + window_size]
                normalized_window = "\n".join(
                    cls._normalize_spaces(line).lower().strip()
                    for line in window_lines
                )
                score = difflib.SequenceMatcher(
                    None, normalized_search, normalized_window
                ).ratio()
                if score <= 0.6:
                    continue
                scored.append((score, start_idx + 1, "\n".join(window_lines)))

            scored.sort(key=lambda x: x[0], reverse=True)
            suggestions_total = len(scored)
            for score, start_line, text in scored[:max_suggestions]:
                suggestions.append(
                    {
                        "line": start_line,
                        "end_line": start_line + len(search_lines) - 1,
                        "text": text,
                        "reason": "multiline difflib similarity",
                        "score": round(score, 4),
                    }
                )
            return suggestions, suggestions_total

        search_tokens = cls._normalize_spaces(search).lower().strip().split()
        if len(search_tokens) < 2:
            return suggestions, 0

        seen: set[tuple[int, str]] = set()
        suggestions_total = 0

        def contains_subsequence(tokens: list[str], needle: list[str]) -> bool:
            if len(needle) > len(tokens):
                return False
            for start in range(0, len(tokens) - len(needle) + 1):
                if tokens[start:start + len(needle)] == needle:
                    return True
            return False

        for i, line in enumerate(limited_lines, start=1):
            line_tokens = cls._normalize_spaces(line).lower().strip().split()
            if not line_tokens:
                continue
            if not contains_subsequence(line_tokens, search_tokens):
                continue

            key = (i, line)
            if key in seen:
                continue
            seen.add(key)
            suggestions_total += 1
            if len(suggestions) < max_suggestions:
                suggestions.append(
                    {
                        "line": i,
                        "text": line,
                        "reason": "token subsequence match (lowercased, stripped)",
                    }
                )

        return suggestions, suggestions_total

    @staticmethod
    def _build_unified_diff(
        path: str,
        old_content: str,
        new_content: str,
        *,
        context_lines: int,
        max_chars: int,
    ) -> tuple[str, bool]:
        diff_text = "".join(
            line if line.endswith("\n") else f"{line}\n"
            for line in difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=path,
                tofile=path,
                n=context_lines,
                lineterm="\n",
            )
        )
        if len(diff_text) <= max_chars:
            return diff_text, False
        return diff_text[:max_chars], True

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "fs_search_and_replace",
            "description": (
                "Replaces exact text in a file using literal matching. "
                "Uses literal string matching (case-sensitive, no regex). "
                "Supports dry-run, occurrence selection, optional backup, and optional unified diff output. "
                "Always returns a JSON string with fields: ok, applied, matched, replaced, path, message, error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The absolute path of the file to modify. Must be a valid file path; directories are not allowed."
                    },
                    "search": {
                        "type": "string",
                        "description": "The exact content to find and replace. Match is case-sensitive and literal (not regex). May be multiline. Cannot be empty."
                    },
                    "replace": {
                        "type": "string",
                        "description": "The new content to replace the found content with. May be multiline. Can be empty to remove the matched content."
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true, do not modify the file; only report whether replacement would happen."
                    },
                    "expected_matches": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "If provided, tool fails unless the total number of matches in file equals this value."
                    },
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Optional 1-based start line of the search/replace scope."
                    },
                    "end_line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Optional 1-based end line of the search/replace scope."
                    },
                    "occurrence": {
                        "description": "Which occurrence to replace: 'first' (default), 'last', 'all', or 1-based integer index.",
                        "anyOf": [
                            {"type": "string", "enum": ["first", "last", "all"]},
                            {"type": "integer", "minimum": 1}
                        ]
                    },
                    "backup": {
                        "type": "boolean",
                        "description": "If true and file is modified, creates '<path>.bak' before writing."
                    },
                    "return_diff": {
                        "type": "boolean",
                        "description": "If true, include a unified diff in the result."
                    },
                    "diff_context_lines": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Context lines for unified diff when return_diff=true. Default 3."
                    },
                    "max_diff_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Maximum number of characters returned in diff output. Default 12000."
                    },
                    "expected_file_sha256": {
                        "type": "string",
                        "description": (
                            "Optional SHA-256 hex digest from file_info(include_hash=true). "
                            "Edit aborts with stale_file if file content changed."
                        ),
                    },
                },
                "required": ["path", "search", "replace"],
                "additionalProperties": False
            },
            "strict": True
        }

    @classmethod
    def prompt_description(cls) -> str:
        description = '''Tool name: fs_search_and_replace  
Tool description: Replaces exact text in a file using literal matching; supports dry-run, occurrence selection, expected match count, backup, and optional diff output.
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "The absolute path of the file to modify."}, "search": {"type": "string", "description": "The exact content to find and replace.  May be multiline."}, "replace": {"type": "string", "description": "The new content to replace the found content with.  May be multiline."}, "dry_run": {"type": "boolean"}, "expected_matches": {"type": "integer", "minimum": 0}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}, "occurrence": {"anyOf": [{"type": "string", "enum": ["first", "last", "all"]}, {"type": "integer", "minimum": 1}]}, "backup": {"type": "boolean"}, "return_diff": {"type": "boolean"}, "diff_context_lines": {"type": "integer", "minimum": 0}, "max_diff_chars": {"type": "integer", "minimum": 1}}, "required": ["path", "search", "replace"], "additionalProperties": false}

Usage:
- Modifies a **single file** by replacing one occurrence of `search` with `replace`.
- The `search` string must match **exactly** (case-sensitive).
- By default, only the **first match** is replaced. You can choose `occurrence="last"`, `occurrence="all"`, or a 1-based occurrence number.
- File is rewritten safely (write + replace) to prevent data loss.
- Uses UTF-8 encoding with `errors='ignore'` for robustness.
- Optional `dry_run=true` computes result without writing.
- Optional `expected_matches` guards against unintended edits when total match count differs.
- Optional `start_line`/`end_line` limit replacements to a specific line range.
- Optional `backup=true` creates `<path>.bak` before write.
- Optional `return_diff=true` returns unified diff.
- Optional `diff_context_lines` controls context lines in diff (default 3).
- Optional `max_diff_chars` limits diff size in response (default 12000).

Important Usage Rules
- **ALWAYS** use absolute paths for `path`.
- The `search` string is treated as **literal text**, not regex.
- To perform regex replacements, use `fs_search_files` first to locate, then decide on literal replacement.
- If the file does not contain the `search` string, no changes are made.
- If the file is empty or unreadable, an error is raised.
- This tool ALWAYS returns a JSON string with fields:
  - ok: boolean — overall success (true means tool completed successfully, including dry-run preview).
  - applied: boolean — whether file changes were actually written to disk.
  - matched: boolean — whether `search` was found in the file.
  - replaced: integer — number of replacements applied (0 or 1).
  - match_count: integer — total number of matches found in file before replacement.
  - line_numbers: array[integer] — line number of replaced occurrence (or empty).
  - match_ranges: array[object] — matched ranges as {start_line, end_line}.
  - backup_path: string|null — backup file path when backup is created.
  - diff: string|null — unified diff when return_diff=true.
  - path: string — the file path.
  - message: string — human-readable summary.
  - error: object|null — error details when ok=false due to an error (not "not found").
- **NEVER** use shell commands like `sed`, `perl -pi`, etc. — this tool is safe and cross-platform.
- For multiline replacements use line feed characters (e.g. \\n, \\r\\n depending on the platform)  in literal text for arguments `search` and `replace`

Examples
Replace database URL in config:
{
  "path": "/home/user/app/config.json",
  "search": "localhost:5432",
  "replace": "db.prod.internal:5432"
}

Fix a typo in source code:
{
  "path": "/home/user/project/main.py",
  "search": "prin('Hello')",
  "replace": "print('Hello')"
}

Update version string:
{
  "path": "/home/user/project/README.md",
  "search": "Version\\n1.0.0",
  "replace": "Version\\n1.0.1"
}
'''
        return description

    @staticmethod
    def _result(
        *,
        path: str,
        ok: bool,
        applied: bool,
        matched: bool,
        replaced: int,
        match_count: int,
        line_numbers: list[int] | None,
        message: str,
        match_ranges: list[Dict[str, int]] | None = None,
        backup_path: str | None = None,
        diff: str | None = None,
        diff_truncated: bool = False,
        error: Dict[str, Any] | None = None,
        suggestions: list[Dict[str, Any]] | None = None,
        suggestions_total: int = 0,
    ) -> str:
        return json.dumps(
            {
                "tool": SearchAndReplace.registered_tool_name(),
                "ok": ok,
                "applied": applied,
                "matched": matched,
                "replaced": replaced,
                "match_count": match_count,
                "line_numbers": line_numbers or [],
                "match_ranges": match_ranges or [],
                "path": tool_report_abspath(path),
                "backup_path": tool_report_abspath(backup_path),
                "diff": diff,
                "diff_truncated": diff_truncated,
                "suggestions": suggestions or [],
                "suggestions_total": suggestions_total,
                "error": error,
                "message": message,
            },
            ensure_ascii=False,
        )

    def _root_box_blocked_result(
        self,
        error: Dict[str, Any],
        path: str,
        search: str,
        replace: str,
        **kwargs,
    ) -> str:
        return self._result(
            path=path,
            ok=False,
            applied=False,
            matched=False,
            replaced=0,
            match_count=0,
            line_numbers=[],
            message=error["message"],
            suggestions=[],
            suggestions_total=0,
            error=error,
        )

    @enforce_root_box(path_arg="path", operation="search_and_replace")
    def __call__(
        self,
        path: str,
        search: str,
        replace: str,
        dry_run: bool = False,
        expected_matches: int | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        occurrence: str | int = "first",
        backup: bool = False,
        return_diff: bool = False,
        diff_context_lines: int = default_diff_context_lines,
        max_diff_chars: int = default_max_diff_chars,
        expected_file_sha256: str | None = None,
        **kwargs,
    ) -> str:
        _result = functools.partial(self._result, path=path, applied=False)

        # Input validation
        if not isinstance(path, str):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'path' must be a string.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "path"},
            )
        if not isinstance(search, str):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'search' must be a string.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "search"},
            )
        if not isinstance(replace, str):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'replace' must be a string.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "replace"},
            )
        if not isinstance(dry_run, bool):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'dry_run' must be a boolean.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "dry_run"},
            )
        if expected_matches is not None and (
            not isinstance(expected_matches, int) or expected_matches < 0
        ):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'expected_matches' must be a non-negative integer.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "expected_matches"},
            )
        if start_line is not None and (
            not isinstance(start_line, int) or start_line < 1
        ):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'start_line' must be a positive integer.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "start_line"},
            )
        if end_line is not None and (
            not isinstance(end_line, int) or end_line < 1
        ):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'end_line' must be a positive integer.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "end_line"},
            )
        expected_total_matches = expected_matches
        if not (
            occurrence in ("first", "last", "all")
            or (isinstance(occurrence, int) and occurrence >= 1)
        ):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'occurrence' must be 'first', 'last', 'all', or a 1-based integer.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "occurrence"},
            )
        if occurrence == "all" and expected_total_matches is None:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'expected_matches' is required when occurrence='all'.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "expected_matches"},
            )
        if not isinstance(backup, bool):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'backup' must be a boolean.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "backup"},
            )
        if not isinstance(return_diff, bool):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'return_diff' must be a boolean.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "return_diff"},
            )
        if not isinstance(diff_context_lines, int) or diff_context_lines < 0:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'diff_context_lines' must be a non-negative integer.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "diff_context_lines"},
            )
        if not isinstance(max_diff_chars, int) or max_diff_chars < 1:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'max_diff_chars' must be a positive integer.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "max_diff_chars"},
            )
        if expected_file_sha256 is not None:
            if not isinstance(expected_file_sha256, str) or not is_valid_sha256_hex(
                expected_file_sha256
            ):
                return _result(
                    ok=False,
                    matched=False,
                    replaced=0,
                    match_count=0,
                    line_numbers=[],
                    message="Argument 'expected_file_sha256' must be a 64-character hex SHA-256 digest.",
                    suggestions=[],
                    suggestions_total=0,
                    error={"type": "invalid_argument", "field": "expected_file_sha256"},
                )

        if not path:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'path' cannot be an empty string.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "path"},
            )
        if not os.path.exists(path):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="File not found.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "not_found"},
            )
        if not os.path.isfile(path):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Path is not a file.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "not_a_file"},
            )
        try:
            file_size = os.path.getsize(path)
        except Exception as e:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Failed to get file size.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "stat_failed", "message": str(e)},
            )
        if file_size > self.max_file_size_bytes:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message=(
                    f"File is too large for search_and_replace: {file_size} bytes "
                    f"(max {self.max_file_size_bytes} bytes)."
                ),
                suggestions=[],
                suggestions_total=0,
                error={
                    "type": "file_too_large",
                    "actual_bytes": file_size,
                    "max_bytes": self.max_file_size_bytes,
                },
            )

        if search == '':
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'search' cannot be an empty string.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "search"},
            )

        try:
            content = read_text_for_edit(path)
        except Exception as e:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Failed to read file.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "read_failed", "message": str(e)},
            )

        stale_error = check_expected_file_sha256(content, expected_file_sha256)
        if stale_error is not None:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message=stale_error["message"],
                suggestions=[],
                suggestions_total=0,
                error=stale_error,
            )

        total_lines = content.count("\n") + 1
        effective_start_line = start_line if start_line is not None else 1
        effective_end_line = end_line if end_line is not None else total_lines
        if effective_start_line > effective_end_line:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message="Argument 'start_line' must be <= 'end_line'.",
                suggestions=[],
                suggestions_total=0,
                error={"type": "invalid_argument", "field": "start_line"},
            )

        # Build all in-scope match starts (non-overlapping, like str.replace behavior).
        match_entries: list[tuple[int, int, int]] = []  # (start_idx, start_line, end_line)
        start = 0
        while True:
            idx = content.find(search, start)
            if idx == -1:
                break
            matched_fragment = content[idx:idx + len(search)]
            match_start_line = content.count("\n", 0, idx) + 1
            match_end_line = match_start_line + matched_fragment.count("\n")
            if (
                match_start_line >= effective_start_line
                and match_end_line <= effective_end_line
            ):
                match_entries.append((idx, match_start_line, match_end_line))
            start = idx + len(search)

        match_count = len(match_entries)
        if match_count == 0:
            suggestions, suggestions_total = self._build_simple_suggestions(search=search, content=content)
            miss_message = "Search string not found in file. No changes made."
            if start_line is not None or end_line is not None:
                miss_message = (
                    f"Search string not found in specified line range "
                    f"{effective_start_line}-{effective_end_line}. No changes made."
                )
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message=miss_message,
                suggestions=suggestions,
                suggestions_total=suggestions_total,
                error=None,
            )

        if expected_total_matches is not None and match_count != expected_total_matches:
            mismatch_line_numbers = sorted({entry[1] for entry in match_entries})
            mismatch_match_ranges = [
                {"start_line": entry[1], "end_line": entry[2]}
                for entry in match_entries
            ]
            return _result(
                ok=False,
                matched=True,
                replaced=0,
                match_count=match_count,
                line_numbers=mismatch_line_numbers,
                match_ranges=mismatch_match_ranges,
                message=(
                    f"Match count mismatch: expected {expected_total_matches}, found {match_count}. "
                    "No changes made."
                ),
                suggestions=[],
                suggestions_total=0,
                error={"type": "match_count_mismatch"},
            )

        if occurrence == "all":
            selected_entries = match_entries
        elif occurrence == "first":
            selected_entries = [match_entries[0]]
        elif occurrence == "last":
            selected_entries = [match_entries[-1]]
        else:
            target_idx = occurrence - 1
            if target_idx < 0 or target_idx >= match_count:
                return _result(
                    ok=False,
                    matched=True,
                    replaced=0,
                    match_count=match_count,
                    line_numbers=[],
                    message=f"Occurrence {occurrence} is out of range for {match_count} matches. No changes made.",
                    suggestions=[],
                    suggestions_total=0,
                    error={"type": "occurrence_out_of_range"},
                )
            selected_entries = [match_entries[target_idx]]

        line_numbers = sorted({entry[1] for entry in selected_entries})
        match_ranges = [
            {"start_line": entry[1], "end_line": entry[2]}
            for entry in selected_entries
        ]
        replaced_count = len(selected_entries)

        parts: list[str] = []
        prev_end = 0
        for start_idx, _, _ in sorted(selected_entries, key=lambda x: x[0]):
            parts.append(content[prev_end:start_idx])
            parts.append(replace)
            prev_end = start_idx + len(search)
        parts.append(content[prev_end:])
        new_content = "".join(parts)
        diff_text = None
        diff_truncated = False
        if return_diff:
            diff_text, diff_truncated = self._build_unified_diff(
                path,
                content,
                new_content,
                context_lines=diff_context_lines,
                max_chars=max_diff_chars,
            )

        if new_content == content:
            return _result(
                ok=False,
                matched=True,
                replaced=0,
                match_count=match_count,
                line_numbers=line_numbers,
                match_ranges=match_ranges,
                message="No change needed (content identical after replacement).",
                diff=diff_text,
                diff_truncated=diff_truncated,
                suggestions=[],
                suggestions_total=0,
                error=None,
            )

        if dry_run:
            return _result(
                ok=True,
                matched=True,
                replaced=0,
                match_count=match_count,
                line_numbers=line_numbers,
                match_ranges=match_ranges,
                message="Dry run: replacement preview generated. No changes made.",
                diff=diff_text,
                diff_truncated=diff_truncated,
                suggestions=[],
                suggestions_total=0,
                error=None,
            )

        tmp_path = f"{path}.tmp"
        backup_path = None
        if backup:
            backup_path = f"{path}.bak"
            try:
                with open(path, "rb") as src, open(backup_path, "wb") as dst:
                    dst.write(src.read())
            except Exception as e:
                return _result(
                    ok=False,
                    matched=True,
                    replaced=0,
                    match_count=match_count,
                    line_numbers=[],
                    message="Failed to create backup file.",
                    suggestions=[],
                    suggestions_total=0,
                    error={"type": "backup_failed", "message": str(e)},
                )

        try:
            with open(tmp_path, 'w', encoding='utf-8', errors='replace') as f:
                f.write(new_content)
            os.replace(tmp_path, path)
        except Exception as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return _result(
                ok=False,
                matched=True,
                replaced=0,
                match_count=match_count,
                line_numbers=line_numbers,
                match_ranges=match_ranges,
                message="Failed to write file.",
                backup_path=backup_path,
                diff=diff_text,
                diff_truncated=diff_truncated,
                suggestions=[],
                suggestions_total=0,
                error={"type": "write_failed", "message": str(e)},
            )

        return _result(
            ok=True,
            applied=True,
            matched=True,
            replaced=replaced_count,
            match_count=match_count,
            line_numbers=line_numbers,
            match_ranges=match_ranges,
            backup_path=backup_path,
            diff=diff_text,
            diff_truncated=diff_truncated,
            message="Text replaced successfully.",
            suggestions=[],
            suggestions_total=0,
            error=None,
        )
