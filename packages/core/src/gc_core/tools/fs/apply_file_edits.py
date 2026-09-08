import difflib
import functools
import json
from typing import Any, Dict, List, Optional

from gc_core.security.path_policy import enforce_write_guard
from gc_core.utils.file_text import (
    check_expected_file_sha256,
    decode_text_bytes,
    is_valid_sha256_hex,
    sha256_hex_of_bytes,
    try_encode_text_bytes,
)
from gc_core.utils.fs_size_limits import check_file_size, fs_mutate_max_bytes
from gc_core.tools.fs.edit_line_endings import (
    collect_literal_matches,
    line_ending_change_refused_error,
    line_ending_change_refused_message,
    resolve_edit_line_endings,
)
from gc_core.tools.fs.edit_suggestions import build_edit_miss_suggestions
from gc_core.tools.schema_optional import coalesce_optional_string
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.tool_report_paths import tool_report_abspath

class ApplyFileEdits(AbstractSimpleTool):
    default_diff_context_lines = 3
    default_max_diff_chars = 12000

    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "fs_apply_file_edits",
            "description": (
                "Apply multiple literal search/replace edits to one file atomically. "
                "All edits are validated and applied in-memory first; if any edit fails, file is unchanged."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "search": {
                                    "type": "string",
                                    "description": "The exact content to find and replace. Use a unique multi-line snippet to avoid accidental matches elsewhere in the file."
                                },
                                "replace": {"type": "string"},
                                "expected_matches": {
                                    "anyOf": [
                                        {"type": "integer", "minimum": 0},
                                        {"type": "null"},
                                    ]
                                },
                                "occurrence": {
                                    "anyOf": [
                                        {"type": "string", "enum": ["first", "last", "all"]},
                                        {"type": "integer", "minimum": 1},
                                        {"type": "null"},
                                    ]
                                },
                            },
                            "required": [
                                "search",
                                "replace",
                            ],
                            "x-optional-keys": [
                                "expected_matches",
                                "occurrence",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "dry_run": {"type": "boolean"},
                    "backup": {"type": "boolean"},
                    "return_diff": {"type": "boolean"},
                    "diff_context_lines": {"type": "integer", "minimum": 0},
                    "max_diff_chars": {"type": "integer", "minimum": 1},
                    "expected_file_sha256": {
                        "type": "string",
                        "description": (
                            "SHA-256 hex digest from file_info(include_hash=true). "
                            "Batch aborts with stale_file if file content changed."
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
                "required": ["path", "edits"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> str:
        return (
            "Tool name: fs_apply_file_edits\n"
            "Apply multiple exact literal edits to one file atomically.\n"
            "Matching is done across the ENTIRE file — there are no start_line/end_line scope args.\n"
            "For disambiguation use a wide enough unique snippet in search (with surrounding context),\n"
            "or use occurrence/first/last/all and expected_matches.\n"
            "Short search like 'i += 1' may match unseen duplicates in file parts not in current read page;\n"
            "prefer broader unique fragments and guard with expected_matches=1 when appropriate.\n"
            "Write search/replace with \\n; the file's CRLF/LF style is kept.\n"
            "If any edit fails validation or matching, no file changes are written.\n"
            "edits_would_apply/total_would_replace count in-memory simulation;\n"
            "edits_applied/total_replaced are non-zero only when applied=true.\n"
            "Pass expected_file_sha256 from file_info(include_hash=true) to detect stale readback.\n"
        )

    def _result(self, 
        *,
        path: str,
        ok: bool,
        applied: bool,
        edits_would_apply: int,
        total_would_replace: int,
        edits_applied: int,
        total_replaced: int,
        edit_results: List[Dict[str, Any]],
        message: str,
        failed_edit_index: int | None = None,
        backup_path: str | None = None,
        diff: str | None = None,
        diff_truncated: bool = False,
        error: Dict[str, Any] | None = None,
        sha256: str | None = None,
        encoding_used: str | None = None,
    ) -> str:
        return json.dumps(
            {
                "tool": ApplyFileEdits.registered_tool_name(),
                "ok": ok,
                "applied": applied,
                "path": tool_report_abspath(path, workspace=self._workspace),
                "edits_would_apply": edits_would_apply,
                "total_would_replace": total_would_replace,
                "edits_applied": edits_applied,
                "total_replaced": total_replaced,
                "edit_results": edit_results,
                "failed_edit_index": failed_edit_index,
                "backup_path": tool_report_abspath(backup_path, workspace=self._workspace),
                "diff": diff,
                "diff_truncated": diff_truncated,
                "message": message,
                "sha256": sha256,
                "encoding_used": encoding_used,
                "error": error,
            },
            ensure_ascii=False,
        )


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

    def _apply_one_edit(
        self,
        *,
        content: str,
        edit: Dict[str, Any],
    ) -> tuple[bool, str, Dict[str, Any], Dict[str, Any] | None, bool]:
        resolved = resolve_edit_line_endings(content, edit["search"], edit["replace"])
        search = resolved.search
        replace = resolved.replace
        occurrence = edit.get("occurrence")
        if occurrence is None:
            occurrence = "first"
        expected_matches = edit.get("expected_matches")
        matches = collect_literal_matches(content, search, resolved.first_index)
        match_count = len(matches)
        if match_count == 0:
            suggestions, _suggestions_total = build_edit_miss_suggestions(
                search=edit["search"],
                content=content,
            )
            return False, content, {
                "matched": False,
                "replaced": 0,
                "match_count": 0,
                "line_numbers": [],
                "match_ranges": [],
                "suggestions": suggestions,
            }, {"type": "not_found"}, False

        if expected_matches is not None and match_count != expected_matches:
            ranges = [{"start_line": m[1], "end_line": m[2]} for m in matches]
            return False, content, {
                "matched": True,
                "replaced": 0,
                "match_count": match_count,
                "line_numbers": sorted({m[1] for m in matches}),
                "match_ranges": ranges,
            }, {"type": "match_count_mismatch"}, False

        if occurrence == "all":
            selected = matches
        elif occurrence == "first":
            selected = [matches[0]]
        elif occurrence == "last":
            selected = [matches[-1]]
        elif isinstance(occurrence, int):
            i = occurrence - 1
            if i < 0 or i >= match_count:
                return False, content, {
                    "matched": True,
                    "replaced": 0,
                    "match_count": match_count,
                    "line_numbers": [],
                    "match_ranges": [],
                }, {"type": "occurrence_out_of_range"}, False
            selected = [matches[i]]
        else:
            return False, content, {
                "matched": False,
                "replaced": 0,
                "match_count": 0,
                "line_numbers": [],
                "match_ranges": [],
            }, {"type": "invalid_argument", "field": "occurrence"}, False

        parts: list[str] = []
        prev_end = 0
        for idx, _, _ in sorted(selected, key=lambda x: x[0]):
            parts.append(content[prev_end:idx])
            parts.append(replace)
            prev_end = idx + len(search)
        parts.append(content[prev_end:])
        new_content = "".join(parts)
        ranges = [{"start_line": m[1], "end_line": m[2]} for m in selected]
        meta: Dict[str, Any] = {
            "matched": True,
            "replaced": len(selected),
            "match_count": match_count,
            "line_numbers": sorted({m[1] for m in selected}),
            "match_ranges": ranges,
        }
        return True, new_content, meta, None, resolved.eol_adapted

    async def acall(self, *args, **kwargs) -> str:
        """Инлайн на loop: нативного async у файлов нет (docs/async.md)."""
        return self(*args, **kwargs)

    @enforce_write_guard(path_arg="path", operation="write")
    def __call__(
        self,
        path: str,
        edits: list,
        atomic: bool = True,
        dry_run: bool = False,
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
        _result = functools.partial(self._result, path=path)
        if not isinstance(path, str):
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Argument 'path' must be a string.", error={"type": "invalid_argument", "field": "path"})
        if not isinstance(edits, list) or len(edits) == 0:
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Argument 'edits' must be a non-empty array.", error={"type": "invalid_argument", "field": "edits"})
        if not isinstance(atomic, bool) or not atomic:
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Argument 'atomic' must be true.", error={"type": "invalid_argument", "field": "atomic"})
        if not isinstance(dry_run, bool):
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Argument 'dry_run' must be a boolean.", error={"type": "invalid_argument", "field": "dry_run"})
        if not isinstance(backup, bool):
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Argument 'backup' must be a boolean.", error={"type": "invalid_argument", "field": "backup"})
        if not isinstance(return_diff, bool):
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Argument 'return_diff' must be a boolean.", error={"type": "invalid_argument", "field": "return_diff"})
        if not isinstance(diff_context_lines, int) or diff_context_lines < 0:
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Argument 'diff_context_lines' must be a non-negative integer.", error={"type": "invalid_argument", "field": "diff_context_lines"})
        if not isinstance(max_diff_chars, int) or max_diff_chars < 1:
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Argument 'max_diff_chars' must be a positive integer.", error={"type": "invalid_argument", "field": "max_diff_chars"})
        if expected_file_sha256 is not None:
            if not isinstance(expected_file_sha256, str) or not is_valid_sha256_hex(expected_file_sha256):
                return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Argument 'expected_file_sha256' must be a 64-character hex SHA-256 digest.", error={"type": "invalid_argument", "field": "expected_file_sha256"})

        if not self._backend.exists(path):
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="File not found.", error={"type": "not_found"})
        if not self._backend.is_file(path):
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Path is not a file.", error={"type": "not_a_file"})
        try:
            file_size = self._backend.getsize(path)
        except Exception as e:
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Failed to get file size.", error={"type": "stat_failed", "message": str(e)})
        size_err = check_file_size(
            file_size,
            fs_mutate_max_bytes(self._config_dict),
            operation="apply_file_edits",
        )
        if size_err is not None:
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message=size_err["message"], error=size_err)

        try:
            raw = self._backend.read_bytes(path)
        except Exception as e:
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Failed to read file.", error={"type": "read_failed", "message": str(e)})
        decoded = decode_text_bytes(raw, encoding=encoding)
        if not decoded.ok:
            err = decoded.error or {"type": "read_failed", "message": "Failed to decode file."}
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message=err.get("message", "Failed to decode file."), error=err)
        original = decoded.text or ""
        encoding_used = decoded.encoding_used
        actual_sha256 = sha256_hex_of_bytes(raw)
        stale_error = check_expected_file_sha256(actual_sha256, expected_file_sha256)
        if stale_error is not None:
            return _result(
                ok=False,
                applied=False,
                edits_would_apply=0,
                total_would_replace=0,
                edits_applied=0,
                total_replaced=0,
                edit_results=[],
                message=stale_error["message"],
                error=stale_error,
            )

        current = original
        edit_results: list[Dict[str, Any]] = []
        edits_would_apply = 0
        total_would_replace = 0
        any_eol_adapted = False
        for i, edit in enumerate(edits):
            if not isinstance(edit, dict):
                return _result(ok=False, applied=False, edits_would_apply=edits_would_apply, total_would_replace=total_would_replace, edits_applied=0, total_replaced=0, edit_results=edit_results, failed_edit_index=i, message=f"Edit #{i} must be an object.", error={"type": "invalid_argument", "field": "edits"})
            if "search" not in edit or "replace" not in edit:
                return _result(ok=False, applied=False, edits_would_apply=edits_would_apply, total_would_replace=total_would_replace, edits_applied=0, total_replaced=0, edit_results=edit_results, failed_edit_index=i, message=f"Edit #{i} must include 'search' and 'replace'.", error={"type": "invalid_argument", "field": "edits"})
            if not isinstance(edit["search"], str) or edit["search"] == "":
                return _result(ok=False, applied=False, edits_would_apply=edits_would_apply, total_would_replace=total_would_replace, edits_applied=0, total_replaced=0, edit_results=edit_results, failed_edit_index=i, message=f"Edit #{i} has invalid 'search'.", error={"type": "invalid_argument", "field": "search"})
            if not isinstance(edit["replace"], str):
                return _result(ok=False, applied=False, edits_would_apply=edits_would_apply, total_would_replace=total_would_replace, edits_applied=0, total_replaced=0, edit_results=edit_results, failed_edit_index=i, message=f"Edit #{i} has invalid 'replace'.", error={"type": "invalid_argument", "field": "replace"})

            ok_edit, next_content, meta, err, eol_adapted = self._apply_one_edit(
                content=current, edit=edit
            )
            meta["index"] = i
            edit_results.append(meta)
            any_eol_adapted = any_eol_adapted or eol_adapted
            if not ok_edit:
                message = f"Edit #{i} failed. No changes applied."
                if err and err.get("type") == "not_found":
                    suggestions = meta.get("suggestions") or []
                    if suggestions:
                        tops = ", ".join(
                            f"line {s['line']} ({s['score']})" for s in suggestions
                        )
                        message = (
                            f"Edit #{i} failed: search not found. "
                            f"Top matches: {tops}."
                        )
                    else:
                        message = (
                            f"Edit #{i} failed: search not found. "
                            "No changes applied."
                        )
                return _result(
                    ok=False,
                    applied=False,
                    edits_would_apply=edits_would_apply,
                    total_would_replace=total_would_replace,
                    edits_applied=0,
                    total_replaced=0,
                    edit_results=edit_results,
                    failed_edit_index=i,
                    message=message,
                    error=err,
                )
            current = next_content
            edits_would_apply += 1
            total_would_replace += meta["replaced"]

        diff_text = None
        diff_truncated = False
        if return_diff:
            diff_text, diff_truncated = self._build_unified_diff(
                path,
                original,
                current,
                context_lines=diff_context_lines,
                max_chars=max_diff_chars,
            )

        if dry_run:
            return _result(
                ok=True,
                applied=False,
                edits_would_apply=edits_would_apply,
                total_would_replace=total_would_replace,
                edits_applied=0,
                total_replaced=0,
                edit_results=edit_results,
                message="Dry run: batch preview generated. No changes made.",
                diff=diff_text,
                diff_truncated=diff_truncated,
                error=None,
            )

        if current == original:
            if any_eol_adapted:
                return _result(
                    ok=False,
                    applied=False,
                    edits_would_apply=edits_would_apply,
                    total_would_replace=total_would_replace,
                    edits_applied=0,
                    total_replaced=0,
                    edit_results=edit_results,
                    message=line_ending_change_refused_message(),
                    diff=diff_text,
                    diff_truncated=diff_truncated,
                    error=line_ending_change_refused_error(),
                )
            return _result(
                ok=True,
                applied=False,
                edits_would_apply=edits_would_apply,
                total_would_replace=total_would_replace,
                edits_applied=0,
                total_replaced=0,
                edit_results=edit_results,
                message="No change needed after batch.",
                diff=diff_text,
                diff_truncated=diff_truncated,
                error=None,
            )

        backup_path = None
        if backup:
            backup_path = f"{path}.bak"
            try:
                self._backend.copy(path, backup_path)
            except Exception as e:
                return _result(
                    ok=False,
                    applied=False,
                    edits_would_apply=edits_would_apply,
                    total_would_replace=total_would_replace,
                    edits_applied=0,
                    total_replaced=0,
                    edit_results=edit_results,
                    message="Failed to create backup file.",
                    error={"type": "backup_failed", "message": str(e)},
                )

        try:
            out_bytes, encode_err = try_encode_text_bytes(current, encoding_used or "utf-8")
            if encode_err is not None:
                return _result(
                    ok=False,
                    applied=False,
                    edits_would_apply=edits_would_apply,
                    total_would_replace=total_would_replace,
                    edits_applied=0,
                    total_replaced=0,
                    edit_results=edit_results,
                    message=encode_err.get("message", "Failed to encode file."),
                    backup_path=backup_path,
                    diff=diff_text,
                    diff_truncated=diff_truncated,
                    error=encode_err,
                )
            assert out_bytes is not None
            self._backend.write_bytes(path, out_bytes)
        except Exception as e:
            return _result(
                ok=False,
                applied=False,
                edits_would_apply=edits_would_apply,
                total_would_replace=total_would_replace,
                edits_applied=0,
                total_replaced=0,
                edit_results=edit_results,
                message="Failed to write file.",
                backup_path=backup_path,
                diff=diff_text,
                diff_truncated=diff_truncated,
                error={"type": "write_failed", "message": str(e)},
            )

        content_sha256 = sha256_hex_of_bytes(out_bytes)
        return _result(
            ok=True,
            applied=True,
            sha256=content_sha256,
            encoding_used=encoding_used,
            edits_would_apply=edits_would_apply,
            total_would_replace=total_would_replace,
            edits_applied=edits_would_apply,
            total_replaced=total_would_replace,
            edit_results=edit_results,
            backup_path=backup_path,
            diff=diff_text,
            diff_truncated=diff_truncated,
            message="Batch edits applied successfully.",
            error=None,
        )

