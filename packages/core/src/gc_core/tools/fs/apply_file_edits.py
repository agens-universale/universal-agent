import difflib
import functools
import json
from typing import Any, Dict, List, Optional

from gc_core.security.path_policy import enforce_write_guard
from gc_core.tools.fs.edit_line_endings import (
    collect_literal_matches,
    line_ending_change_refused_error,
    line_ending_change_refused_message,
    resolve_edit_line_endings,
)
from gc_core.tools.fs.edit_suggestions import build_edit_miss_suggestions
from gc_core.tools.schema_optional import coalesce_optional_string
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.file_text import (
    check_expected_file_sha256,
    decode_text_bytes,
    is_valid_sha256_hex,
    sha256_hex_of_bytes,
    try_encode_text_bytes,
)
from gc_core.utils.fs_size_limits import check_file_size, fs_mutate_max_bytes
from gc_core.utils.tool_report_paths import tool_report_abspath


class _LoadError:
    def __init__(self, message: str, error: Dict[str, Any]) -> None:
        self.message = message
        self.error = error


class _EditBatch:
    """Результат последовательного применения правок к тексту (без записи)."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.edit_results: list[Dict[str, Any]] = []
        self.edits_would_apply = 0
        self.total_would_replace = 0
        self.any_eol_adapted = False
        self.failure: tuple[int, str, Dict[str, Any] | None] | None = None


def _batch_arg_error(
    edits, atomic, dry_run, backup, return_diff, diff_context_lines, max_diff_chars, expected_file_sha256
) -> "tuple[str, Dict[str, Any]] | None":
    """(message, error) для первого невалидного аргумента батча либо None."""
    if not isinstance(edits, list) or len(edits) == 0:
        return "Argument 'edits' must be a non-empty array.", {"type": "invalid_argument", "field": "edits"}
    if not isinstance(atomic, bool) or not atomic:
        return "Argument 'atomic' must be true.", {"type": "invalid_argument", "field": "atomic"}
    if not isinstance(dry_run, bool):
        return "Argument 'dry_run' must be a boolean.", {"type": "invalid_argument", "field": "dry_run"}
    if not isinstance(backup, bool):
        return "Argument 'backup' must be a boolean.", {"type": "invalid_argument", "field": "backup"}
    if not isinstance(return_diff, bool):
        return "Argument 'return_diff' must be a boolean.", {"type": "invalid_argument", "field": "return_diff"}
    if not isinstance(diff_context_lines, int) or diff_context_lines < 0:
        return "Argument 'diff_context_lines' must be a non-negative integer.", {
            "type": "invalid_argument",
            "field": "diff_context_lines",
        }
    if not isinstance(max_diff_chars, int) or max_diff_chars < 1:
        return "Argument 'max_diff_chars' must be a positive integer.", {
            "type": "invalid_argument",
            "field": "max_diff_chars",
        }
    if expected_file_sha256 is not None and (
        not isinstance(expected_file_sha256, str) or not is_valid_sha256_hex(expected_file_sha256)
    ):
        return "Argument 'expected_file_sha256' must be a 64-character hex SHA-256 digest.", {
            "type": "invalid_argument",
            "field": "expected_file_sha256",
        }
    return None


def _edit_arg_error(i: int, edit: Any) -> "tuple[str, Dict[str, Any]] | None":
    if not isinstance(edit, dict):
        return f"Edit #{i} must be an object.", {"type": "invalid_argument", "field": "edits"}
    if "search" not in edit or "replace" not in edit:
        return f"Edit #{i} must include 'search' and 'replace'.", {"type": "invalid_argument", "field": "edits"}
    if not isinstance(edit["search"], str) or edit["search"] == "":
        return f"Edit #{i} has invalid 'search'.", {"type": "invalid_argument", "field": "search"}
    if not isinstance(edit["replace"], str):
        return f"Edit #{i} has invalid 'replace'.", {"type": "invalid_argument", "field": "replace"}
    return None


def _edit_failure_message(i: int, err: "Dict[str, Any] | None", meta: Dict[str, Any]) -> str:
    if not (err and err.get("type") == "not_found"):
        return f"Edit #{i} failed. No changes applied."
    suggestions = meta.get("suggestions") or []
    if suggestions:
        tops = ", ".join(f"line {s['line']} ({s['score']})" for s in suggestions)
        return f"Edit #{i} failed: search not found. Top matches: {tops}."
    return f"Edit #{i} failed: search not found. No changes applied."


def _select_matches(matches: list, occurrence: Any) -> tuple:
    """Выбранные вхождения по occurrence либо (None, (meta, error)) при ошибке."""
    match_count = len(matches)
    if occurrence == "all":
        return matches, None
    if occurrence == "first":
        return [matches[0]], None
    if occurrence == "last":
        return [matches[-1]], None
    if isinstance(occurrence, int):
        i = occurrence - 1
        if i < 0 or i >= match_count:
            return None, (
                {
                    "matched": True,
                    "replaced": 0,
                    "match_count": match_count,
                    "line_numbers": [],
                    "match_ranges": [],
                },
                {"type": "occurrence_out_of_range"},
            )
        return [matches[i]], None
    return None, (
        {
            "matched": False,
            "replaced": 0,
            "match_count": 0,
            "line_numbers": [],
            "match_ranges": [],
        },
        {"type": "invalid_argument", "field": "occurrence"},
    )


def _splice_replacements(content: str, selected: list, search: str, replace: str) -> str:
    parts: list[str] = []
    prev_end = 0
    for idx, _, _ in sorted(selected, key=lambda x: x[0]):
        parts.append(content[prev_end:idx])
        parts.append(replace)
        prev_end = idx + len(search)
    parts.append(content[prev_end:])
    return "".join(parts)


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
                                    "description": "The exact content to find and replace. Use a unique multi-line snippet to avoid accidental matches elsewhere in the file.",
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

    def _result(
        self,
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
            return (
                False,
                content,
                {
                    "matched": False,
                    "replaced": 0,
                    "match_count": 0,
                    "line_numbers": [],
                    "match_ranges": [],
                    "suggestions": suggestions,
                },
                {"type": "not_found"},
                False,
            )

        if expected_matches is not None and match_count != expected_matches:
            ranges = [{"start_line": m[1], "end_line": m[2]} for m in matches]
            return (
                False,
                content,
                {
                    "matched": True,
                    "replaced": 0,
                    "match_count": match_count,
                    "line_numbers": sorted({m[1] for m in matches}),
                    "match_ranges": ranges,
                },
                {"type": "match_count_mismatch"},
                False,
            )

        selected, select_error = _select_matches(matches, occurrence)
        if select_error is not None:
            return False, content, select_error[0], select_error[1], False

        new_content = _splice_replacements(content, selected, search, replace)
        ranges = [{"start_line": m[1], "end_line": m[2]} for m in selected]
        meta: Dict[str, Any] = {
            "matched": True,
            "replaced": len(selected),
            "match_count": match_count,
            "line_numbers": sorted({m[1] for m in selected}),
            "match_ranges": ranges,
        }
        return True, new_content, meta, None, resolved.eol_adapted

    async def _load_source(self, path: str, encoding: "str | None", expected_file_sha256: "str | None"):
        """(original_text, encoding_used) либо _LoadError с готовыми message/error."""
        try:
            file_size = await self._backend.getsize(path)
        except Exception as e:
            return _LoadError("Failed to get file size.", {"type": "stat_failed", "message": str(e)})
        size_err = check_file_size(
            file_size,
            fs_mutate_max_bytes(self._config_dict),
            operation="apply_file_edits",
        )
        if size_err is not None:
            return _LoadError(size_err["message"], size_err)
        try:
            raw = await self._backend.read_bytes(path)
        except Exception as e:
            return _LoadError("Failed to read file.", {"type": "read_failed", "message": str(e)})
        decoded = decode_text_bytes(raw, encoding=encoding)
        if not decoded.ok:
            err = decoded.error or {"type": "read_failed", "message": "Failed to decode file."}
            return _LoadError(err.get("message", "Failed to decode file."), err)
        stale_error = check_expected_file_sha256(sha256_hex_of_bytes(raw), expected_file_sha256)
        if stale_error is not None:
            return _LoadError(stale_error["message"], stale_error)
        return decoded.text or "", decoded.encoding_used

    def _apply_edit_batch(self, edits: list, original: str) -> _EditBatch:
        batch = _EditBatch(original)
        for i, edit in enumerate(edits):
            arg_error = _edit_arg_error(i, edit)
            if arg_error is not None:
                batch.failure = (i, arg_error[0], arg_error[1])
                return batch
            ok_edit, next_content, meta, err, eol_adapted = self._apply_one_edit(content=batch.content, edit=edit)
            meta["index"] = i
            batch.edit_results.append(meta)
            batch.any_eol_adapted = batch.any_eol_adapted or eol_adapted
            if not ok_edit:
                batch.failure = (i, _edit_failure_message(i, err, meta), err)
                return batch
            batch.content = next_content
            batch.edits_would_apply += 1
            batch.total_would_replace += meta["replaced"]
        return batch

    async def _encode_and_write(self, path: str, current: str, encoding_used: "str | None"):
        """(bytes, None) либо (None, (message, error))."""
        try:
            out_bytes, encode_err = try_encode_text_bytes(current, encoding_used or "utf-8")
            if encode_err is not None:
                return None, (encode_err.get("message", "Failed to encode file."), encode_err)
            assert out_bytes is not None
            await self._backend.write_bytes(path, out_bytes)
        except Exception as e:
            return None, ("Failed to write file.", {"type": "write_failed", "message": str(e)})
        return out_bytes, None

    @enforce_write_guard(path_arg="path", operation="write")
    async def acall(
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
        fail = functools.partial(
            _result,
            ok=False,
            applied=False,
            edits_would_apply=0,
            total_would_replace=0,
            edits_applied=0,
            total_replaced=0,
            edit_results=[],
        )
        arg_error = _batch_arg_error(
            edits, atomic, dry_run, backup, return_diff, diff_context_lines, max_diff_chars, expected_file_sha256
        )
        if not isinstance(path, str):
            return fail(
                message="Argument 'path' must be a string.", error={"type": "invalid_argument", "field": "path"}
            )
        if arg_error is not None:
            return fail(message=arg_error[0], error=arg_error[1])

        if not await self._backend.exists(path):
            return fail(message="File not found.", error={"type": "not_found"})
        if not await self._backend.is_file(path):
            return fail(message="Path is not a file.", error={"type": "not_a_file"})
        loaded = await self._load_source(path, encoding, expected_file_sha256)
        if isinstance(loaded, _LoadError):
            return fail(message=loaded.message, error=loaded.error)
        original, encoding_used = loaded

        batch = self._apply_edit_batch(edits, original)
        edit_results = batch.edit_results
        edits_would_apply = batch.edits_would_apply
        total_would_replace = batch.total_would_replace
        if batch.failure is not None:
            failed_index, message, err = batch.failure
            return _result(
                ok=False,
                applied=False,
                edits_would_apply=edits_would_apply,
                total_would_replace=total_would_replace,
                edits_applied=0,
                total_replaced=0,
                edit_results=edit_results,
                failed_edit_index=failed_index,
                message=message,
                error=err,
            )
        current = batch.content
        any_eol_adapted = batch.any_eol_adapted

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

        partial = functools.partial(
            _result,
            ok=False,
            applied=False,
            edits_would_apply=edits_would_apply,
            total_would_replace=total_would_replace,
            edits_applied=0,
            total_replaced=0,
            edit_results=edit_results,
        )
        backup_path = None
        if backup:
            backup_path = f"{path}.bak"
            try:
                await self._backend.copy(path, backup_path)
            except Exception as e:
                return partial(
                    message="Failed to create backup file.",
                    error={"type": "backup_failed", "message": str(e)},
                )

        out_bytes, write_error = await self._encode_and_write(path, current, encoding_used)
        if write_error is not None:
            return partial(
                message=write_error[0],
                backup_path=backup_path,
                diff=diff_text,
                diff_truncated=diff_truncated,
                error=write_error[1],
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
