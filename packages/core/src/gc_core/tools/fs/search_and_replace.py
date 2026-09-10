import json
import functools
import difflib
from typing import Dict, Any, Optional
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.tools.fs.edit_line_endings import (
    collect_literal_matches,
    line_ending_change_refused_error,
    line_ending_change_refused_message,
    resolve_edit_line_endings,
)
from gc_core.tools.fs.edit_suggestions import build_edit_miss_suggestions
from gc_core.utils.tool_report_paths import tool_report_abspath
from gc_core.security.path_policy import enforce_write_guard
from gc_core.tools.schema_optional import coalesce_optional_string
from gc_core.utils.file_text import (
    check_expected_file_sha256,
    decode_text_bytes,
    is_valid_sha256_hex,
    sha256_hex_of_bytes,
    try_encode_text_bytes,
)
from gc_core.utils.fs_size_limits import check_file_size, fs_mutate_max_bytes

class SearchAndReplace(AbstractSimpleTool):
    """
    Tool: search_and_replace
    Performs a single search and replace operation in a file.
    Only the first occurrence is replaced.
    """
    default_diff_context_lines = 3
    default_max_diff_chars = 12000

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
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "fs_search_and_replace",
            "description": (
                "Replaces exact text in a file using literal matching. "
                "Uses literal string matching (case-sensitive, no regex). "
                "Supports dry-run, occurrence selection, optional backup, and optional unified diff output. "
                "When occurrence='all', expected_matches is required (tool fails without it). "
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
                        "description": "The exact content to find and replace. Match is case-sensitive and literal (not regex). May be multiline. Cannot be empty. Use a unique multi-line snippet to avoid accidental matches elsewhere in the file."
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
                        "description": (
                            "Required when occurrence='all' (tool fails with invalid_argument if omitted). "
                            "Otherwise optional: if provided, tool fails unless the total number of matches "
                            "in the file equals this value."
                        ),
                    },
                    "occurrence": {
                        "description": (
                            "Which occurrence to replace: 'first' (default), 'last', 'all', or 1-based integer index. "
                            "When 'all', you must also pass expected_matches equal to the total match count."
                        ),
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
                            "SHA-256 hex digest from file_info(include_hash=true). "
                            "Edit aborts with stale_file if file content changed."
                        ),
                    },
                    "encoding": {
                        "type": "string",
                        "description": (
                            "Optional text encoding. Omit for UTF-8 (default; also UTF-16 with BOM). "
                            "Otherwise: utf-8, utf-8-sig, cp1251, koi8-r, latin-1, utf-16, "
                            "utf-16-le, utf-16-be (also windows-1251, iso-8859-1). "
                            "Successful edits are written back in the same encoding."
                        ),
                    },
                },
                "required": ["path", "search", "replace"],
                "additionalProperties": False
            },
            "strict": True
        }

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> str:
        description = '''Tool name: fs_search_and_replace  
Tool description: Replaces exact text in a file using literal matching; supports dry-run, occurrence selection, expected match count, backup, and optional diff output.
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "The absolute path of the file to modify."}, "search": {"type": "string", "description": "The exact content to find and replace.  May be multiline."}, "replace": {"type": "string", "description": "The new content to replace the found content with.  May be multiline."}, "dry_run": {"type": "boolean"}, "expected_matches": {"type": "integer", "minimum": 0}, "occurrence": {"anyOf": [{"type": "string", "enum": ["first", "last", "all"]}, {"type": "integer", "minimum": 1}]}, "backup": {"type": "boolean"}, "return_diff": {"type": "boolean"}, "diff_context_lines": {"type": "integer", "minimum": 0}, "max_diff_chars": {"type": "integer", "minimum": 1}}, "required": ["path", "search", "replace"], "additionalProperties": false}

Usage:
- Modifies a **single file** by replacing one occurrence of `search` with `replace`.
- The `search` string must match **exactly** (case-sensitive).
- By default, only the **first match** is replaced. You can choose `occurrence="last"`, `occurrence="all"`, or a 1-based occurrence number.
- When `occurrence="all"`, `expected_matches` is **required** (must equal the total match count); omitting it fails with invalid_argument.
- File is rewritten safely (write + replace) to prevent data loss.
- Optional `dry_run=true` computes result without writing.
- Optional `expected_matches` (always required for `occurrence="all"`) guards against unintended edits when total match count differs.
- Matching is done across the ENTIRE file — there are no start_line/end_line scope args.
- For disambiguation use a wide enough unique snippet in search (with surrounding context),
  or use occurrence/first/last/all and expected_matches.
- Short search like "i += 1" may match unseen duplicates in file parts not in current read page;
  prefer broader unique fragments and guard with expected_matches=1 when appropriate.
- Optional `backup=true` creates `<path>.bak` before write.
- Optional `return_diff=true` returns unified diff.
- Optional `diff_context_lines` controls context lines in diff (default 3).
- Optional `max_diff_chars` limits diff size in response (default 12000).

Important Usage Rules
- **ALWAYS** use absolute paths for `path`.
- The `search` string is treated as **literal text**, not regex.
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
- Write `search`/`replace` with \\n; the file's CRLF/LF style is kept.

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

    def _result(self, 
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
        sha256: str | None = None,
        encoding_used: str | None = None,
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
                "path": tool_report_abspath(path, workspace=self._workspace),
                "backup_path": tool_report_abspath(backup_path, workspace=self._workspace),
                "diff": diff,
                "diff_truncated": diff_truncated,
                "suggestions": suggestions or [],
                "suggestions_total": suggestions_total,
                "sha256": sha256,
                "encoding_used": encoding_used,
                "error": error,
                "message": message,
            },
            ensure_ascii=False,
        )

    def _write_guard_blocked_result(
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


    @enforce_write_guard(path_arg="path", operation="search_and_replace")
    async def acall(
        self,
        path: str,
        search: str,
        replace: str,
        dry_run: bool = False,
        expected_matches: int | None = None,
        occurrence: str | int = "first",
        backup: bool = False,
        return_diff: bool = False,
        diff_context_lines: int = default_diff_context_lines,
        max_diff_chars: int = default_max_diff_chars,
        expected_file_sha256: str | None = None,
        encoding: str | None = None,
        **kwargs,
    ) -> str:
        expected_file_sha256 = coalesce_optional_string(expected_file_sha256)
        encoding = coalesce_optional_string(encoding)
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
        if not await self._backend.exists(path):
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
        if not await self._backend.is_file(path):
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
            file_size = await self._backend.getsize(path)
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
        size_err = check_file_size(
            file_size,
            fs_mutate_max_bytes(self._config_dict),
            operation="search_and_replace",
        )
        if size_err is not None:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message=size_err["message"],
                suggestions=[],
                suggestions_total=0,
                error=size_err,
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
            raw = await self._backend.read_bytes(path)
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
        decoded = decode_text_bytes(raw, encoding=encoding)
        if not decoded.ok:
            err = decoded.error or {"type": "read_failed", "message": "Failed to decode file."}
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                match_count=0,
                line_numbers=[],
                message=err.get("message", "Failed to decode file."),
                suggestions=[],
                suggestions_total=0,
                error=err,
            )
        content = decoded.text or ""
        encoding_used = decoded.encoding_used
        original_search = search
        resolved = resolve_edit_line_endings(content, search, replace)
        search = resolved.search
        replace = resolved.replace
        actual_sha256 = sha256_hex_of_bytes(raw)
        stale_error = check_expected_file_sha256(actual_sha256, expected_file_sha256)
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

        match_entries = collect_literal_matches(content, search, resolved.first_index)
        match_count = len(match_entries)
        if match_count == 0:
            suggestions, suggestions_total = build_edit_miss_suggestions(
                search=original_search,
                content=content,
            )
            miss_message = "Search string not found in file. No changes made."
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
            if resolved.eol_adapted:
                return _result(
                    ok=False,
                    matched=True,
                    replaced=0,
                    match_count=match_count,
                    line_numbers=line_numbers,
                    match_ranges=match_ranges,
                    message=line_ending_change_refused_message(),
                    diff=diff_text,
                    diff_truncated=diff_truncated,
                    suggestions=[],
                    suggestions_total=0,
                    error=line_ending_change_refused_error(),
                )
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

        backup_path = None
        if backup:
            backup_path = f"{path}.bak"
            try:
                await self._backend.copy(path, backup_path)
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
            out_bytes, encode_err = try_encode_text_bytes(new_content, encoding_used or "utf-8")
            if encode_err is not None:
                return _result(
                    ok=False,
                    matched=True,
                    replaced=0,
                    match_count=match_count,
                    line_numbers=line_numbers,
                    match_ranges=match_ranges,
                    message=encode_err.get("message", "Failed to encode file."),
                    backup_path=backup_path,
                    diff=diff_text,
                    diff_truncated=diff_truncated,
                    suggestions=[],
                    suggestions_total=0,
                    error=encode_err,
                )
            assert out_bytes is not None
            await self._backend.write_bytes(path, out_bytes)
        except Exception as e:
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

        content_sha256 = sha256_hex_of_bytes(out_bytes)
        return _result(
            ok=True,
            applied=True,
            sha256=content_sha256,
            encoding_used=encoding_used,
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
