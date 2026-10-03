import functools
import json
from typing import Any, Dict

from gc_core.security.path_policy import enforce_write_guard
from gc_core.tools.fs.edit_line_endings import (
    collect_literal_matches,
    line_ending_change_refused_error,
    line_ending_change_refused_message,
    resolve_edit_line_endings,
)
from gc_core.tools.fs.edit_suggestions import build_edit_miss_suggestions
from gc_core.tools.fs.literal_edit_common import (
    LoadError,
    build_unified_diff,
    diff_arg_error,
    encode_and_write,
    load_text_source,
    splice_replacements,
)
from gc_core.tools.schema_optional import coalesce_optional_string
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.file_text import sha256_hex_of_bytes
from gc_core.utils.tool_report_paths import tool_report_abspath


def _replace_arg_error(
    path,
    search,
    replace,
    dry_run,
    expected_matches,
    occurrence,
    backup,
    return_diff,
    diff_context_lines,
    max_diff_chars,
    expected_file_sha256,
) -> "tuple[str, Dict[str, Any]] | None":
    """(message, error) для первого невалидного аргумента либо None."""
    return (
        _text_arg_error(path, search, replace, dry_run)
        or _occurrence_arg_error(expected_matches, occurrence)
        or diff_arg_error(
            backup,
            return_diff,
            diff_context_lines,
            max_diff_chars,
            expected_file_sha256,
        )
    )


def _text_arg_error(
    path, search, replace, dry_run
) -> "tuple[str, Dict[str, Any]] | None":
    if not isinstance(path, str):
        return "Argument 'path' must be a string.", {
            "type": "invalid_argument",
            "field": "path",
        }
    if not isinstance(search, str):
        return "Argument 'search' must be a string.", {
            "type": "invalid_argument",
            "field": "search",
        }
    if not isinstance(replace, str):
        return "Argument 'replace' must be a string.", {
            "type": "invalid_argument",
            "field": "replace",
        }
    if not isinstance(dry_run, bool):
        return "Argument 'dry_run' must be a boolean.", {
            "type": "invalid_argument",
            "field": "dry_run",
        }
    return None


def _occurrence_arg_error(
    expected_matches, occurrence
) -> "tuple[str, Dict[str, Any]] | None":
    if expected_matches is not None and (
        not isinstance(expected_matches, int) or expected_matches < 0
    ):
        return "Argument 'expected_matches' must be a non-negative integer.", {
            "type": "invalid_argument",
            "field": "expected_matches",
        }
    if not (
        occurrence in ("first", "last", "all")
        or (isinstance(occurrence, int) and occurrence >= 1)
    ):
        return (
            "Argument 'occurrence' must be 'first', 'last', 'all', or a 1-based integer.",
            {
                "type": "invalid_argument",
                "field": "occurrence",
            },
        )
    if occurrence == "all" and expected_matches is None:
        return (
            "Argument 'expected_matches' is required when occurrence='all'.",
            {
                "type": "invalid_argument",
                "field": "expected_matches",
            },
        )
    return None


def _miss_payload(original_search: str, content: str) -> Dict[str, Any]:
    suggestions, suggestions_total = build_edit_miss_suggestions(
        search=original_search,
        content=content,
    )
    return dict(
        ok=False,
        matched=False,
        replaced=0,
        match_count=0,
        line_numbers=[],
        message="Search string not found in file. No changes made.",
        suggestions=suggestions,
        suggestions_total=suggestions_total,
        error=None,
    )


def _match_error_payload(
    match_entries: list,
    original_search: str,
    content: str,
    expected_total_matches,
) -> "Dict[str, Any] | None":
    """Payload ошибки поиска (нет вхождений / не то число), либо None."""
    if not match_entries:
        return _miss_payload(original_search, content)
    if (
        expected_total_matches is not None
        and len(match_entries) != expected_total_matches
    ):
        return _mismatch_payload(match_entries, expected_total_matches)
    return None


def _mismatch_payload(
    match_entries: list, expected_total_matches: int
) -> Dict[str, Any]:
    match_count = len(match_entries)
    return dict(
        ok=False,
        matched=True,
        replaced=0,
        match_count=match_count,
        line_numbers=sorted({entry[1] for entry in match_entries}),
        match_ranges=[
            {"start_line": entry[1], "end_line": entry[2]}
            for entry in match_entries
        ],
        message=(
            f"Match count mismatch: expected {expected_total_matches}, found {match_count}. No changes made."
        ),
        suggestions=[],
        suggestions_total=0,
        error={"type": "match_count_mismatch"},
    )


def _unchanged_payload(eol_adapted: bool) -> Dict[str, Any]:
    if eol_adapted:
        return dict(
            message=line_ending_change_refused_message(),
            error=line_ending_change_refused_error(),
        )
    return dict(
        message="No change needed (content identical after replacement).",
        error=None,
    )


def _select_entries(match_entries: list, occurrence) -> tuple:
    """Выбранные вхождения либо (None, message) при выходе occurrence за диапазон."""
    match_count = len(match_entries)
    if occurrence == "all":
        return match_entries, None
    if occurrence == "first":
        return [match_entries[0]], None
    if occurrence == "last":
        return [match_entries[-1]], None
    target_idx = occurrence - 1
    if target_idx < 0 or target_idx >= match_count:
        return (
            None,
            f"Occurrence {occurrence} is out of range for {match_count} matches. No changes made.",
        )
    return [match_entries[target_idx]], None


class SearchAndReplace(AbstractSimpleTool):
    """
    Tool: search_and_replace
    Performs a single search and replace operation in a file.
    Only the first occurrence is replaced.
    """

    default_diff_context_lines = 3
    default_max_diff_chars = 12000

    @classmethod
    def tool_description(cls, settings: Any = None) -> Dict[str, Any]:
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
                        "description": "The absolute path of the file to modify. Must be a valid file path; directories are not allowed.",
                    },
                    "search": {
                        "type": "string",
                        "description": "The exact content to find and replace. Match is case-sensitive and literal (not regex). May be multiline. Cannot be empty. Use a unique multi-line snippet to avoid accidental matches elsewhere in the file.",
                    },
                    "replace": {
                        "type": "string",
                        "description": "The new content to replace the found content with. May be multiline. Can be empty to remove the matched content.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true, do not modify the file; only report whether replacement would happen.",
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
                            {
                                "type": "string",
                                "enum": ["first", "last", "all"],
                            },
                            {"type": "integer", "minimum": 1},
                        ],
                    },
                    "backup": {
                        "type": "boolean",
                        "description": "If true and file is modified, creates '<path>.bak' before writing.",
                    },
                    "return_diff": {
                        "type": "boolean",
                        "description": "If true, include a unified diff in the result.",
                    },
                    "diff_context_lines": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Context lines for unified diff when return_diff=true. Default 3.",
                    },
                    "max_diff_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Maximum number of characters returned in diff output. Default 12000.",
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
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls, settings: Any = None) -> str:
        description = """Tool name: fs_search_and_replace
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
"""
        return description

    def _result(  # noqa: PLR0913  # S107: won't fix, см. CONTRIBUTING.md
        self,
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
                "backup_path": tool_report_abspath(
                    backup_path, workspace=self._workspace
                ),
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

    def _diff_if_requested(
        self,
        return_diff: bool,
        path,
        old: str,
        new: str,
        context_lines: int,
        max_chars: int,
    ):
        if not return_diff:
            return None, False
        return build_unified_diff(
            path, old, new, context_lines=context_lines, max_chars=max_chars
        )

    async def _maybe_backup(
        self, path: str, backup: bool
    ) -> "tuple[str | None, dict | None]":
        if not backup:
            return None, None
        backup_path = f"{path}.bak"
        try:
            await self._backend.copy(path, backup_path)
        except Exception as e:
            return backup_path, {"type": "backup_failed", "message": str(e)}
        return backup_path, None

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
        fail = functools.partial(
            _result,
            ok=False,
            matched=False,
            replaced=0,
            match_count=0,
            line_numbers=[],
            suggestions=[],
            suggestions_total=0,
        )

        # Input validation
        arg_error = _replace_arg_error(
            path,
            search,
            replace,
            dry_run,
            expected_matches,
            occurrence,
            backup,
            return_diff,
            diff_context_lines,
            max_diff_chars,
            expected_file_sha256,
        )
        if arg_error is not None:
            return fail(message=arg_error[0], error=arg_error[1])
        expected_total_matches = expected_matches

        if not path:
            return fail(
                message="Argument 'path' cannot be an empty string.",
                error={"type": "invalid_argument", "field": "path"},
            )
        if not await self._backend.exists(path):
            return fail(message="File not found.", error={"type": "not_found"})
        if not await self._backend.is_file(path):
            return fail(
                message="Path is not a file.", error={"type": "not_a_file"}
            )
        if search == "":
            return fail(
                message="Argument 'search' cannot be an empty string.",
                error={"type": "invalid_argument", "field": "search"},
            )
        loaded = await load_text_source(
            self,
            path,
            operation="search_and_replace",
            encoding=encoding,
            expected_file_sha256=expected_file_sha256,
        )
        if isinstance(loaded, LoadError):
            return fail(message=loaded.message, error=loaded.error)
        content, encoding_used = loaded
        original_search = search
        resolved = resolve_edit_line_endings(content, search, replace)
        search = resolved.search
        replace = resolved.replace

        match_entries = collect_literal_matches(
            content, search, resolved.first_index
        )
        match_count = len(match_entries)
        match_error = _match_error_payload(
            match_entries, original_search, content, expected_total_matches
        )
        if match_error is not None:
            return _result(**match_error)

        selected_entries, select_error = _select_entries(
            match_entries, occurrence
        )
        if select_error is not None:
            return _result(
                ok=False,
                matched=True,
                replaced=0,
                match_count=match_count,
                line_numbers=[],
                message=select_error,
                suggestions=[],
                suggestions_total=0,
                error={"type": "occurrence_out_of_range"},
            )

        line_numbers = sorted({entry[1] for entry in selected_entries})
        match_ranges = [
            {"start_line": entry[1], "end_line": entry[2]}
            for entry in selected_entries
        ]
        replaced_count = len(selected_entries)

        new_content = splice_replacements(
            content, selected_entries, search, replace
        )
        diff_text, diff_truncated = self._diff_if_requested(
            return_diff,
            path,
            content,
            new_content,
            diff_context_lines,
            max_diff_chars,
        )

        if new_content == content:
            return _result(
                ok=False,
                matched=True,
                replaced=0,
                match_count=match_count,
                line_numbers=line_numbers,
                match_ranges=match_ranges,
                diff=diff_text,
                diff_truncated=diff_truncated,
                suggestions=[],
                suggestions_total=0,
                **_unchanged_payload(resolved.eol_adapted),
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

        backup_path, backup_error = await self._maybe_backup(path, backup)
        if backup_error is not None:
            return _result(
                ok=False,
                matched=True,
                replaced=0,
                match_count=match_count,
                line_numbers=[],
                message="Failed to create backup file.",
                suggestions=[],
                suggestions_total=0,
                error=backup_error,
            )

        out_bytes, write_error = await encode_and_write(
            self, path, new_content, encoding_used
        )
        if write_error is not None:
            return _result(
                ok=False,
                matched=True,
                replaced=0,
                match_count=match_count,
                line_numbers=line_numbers,
                match_ranges=match_ranges,
                message=write_error[0],
                backup_path=backup_path,
                diff=diff_text,
                diff_truncated=diff_truncated,
                suggestions=[],
                suggestions_total=0,
                error=write_error[1],
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
