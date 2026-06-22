"""Harbor variant of fs_apply_file_edits.

Faithful 1:1 reimplementation of ``ApplyFileEdits.__call__`` for the External adapter:
same validation, same edit logic (inherited ``_apply_one_edit`` / ``_collect_matches`` /
``_build_unified_diff``), same atomic semantics, same ``_result`` envelope. The ONLY
difference is physical I/O — file existence/size/read/write go through HarborBackend
(the task container) instead of the host filesystem. The base ``@enforce_write_guard``
decorator is intentionally dropped: the container is the sandbox.
"""
import functools

from tools.fs.apply_file_edits import ApplyFileEdits
from tools.schema_optional import coalesce_optional_string
from utils.file_text import check_expected_file_sha256, is_valid_sha256_hex


class HarborApplyFileEdits(ApplyFileEdits):
    def __call__(
        self,
        path: str,
        edits: list,
        atomic: bool = True,
        dry_run: bool = False,
        backup: bool = False,
        return_diff: bool = False,
        diff_context_lines: int = ApplyFileEdits.default_diff_context_lines,
        max_diff_chars: int = ApplyFileEdits.default_max_diff_chars,
        expected_file_sha256: str | None = None,
        **kwargs,
    ) -> str:
        backend = self._ctx.harbor_backend
        expected_file_sha256 = coalesce_optional_string(expected_file_sha256)
        _result = functools.partial(self._result, path=path)

        # --- argument validation (identical to base) ---
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

        # --- file checks + read (I/O swapped to container backend) ---
        if not backend.exists(path):
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="File not found.", error={"type": "not_found"})
        if not backend.is_file(path):
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Path is not a file.", error={"type": "not_a_file"})
        try:
            file_size = backend.getsize(path)
        except Exception as e:
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Failed to get file size.", error={"type": "stat_failed", "message": str(e)})
        if file_size > self.max_file_size_bytes:
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message=f"File is too large for apply_file_edits: {file_size} bytes (max {self.max_file_size_bytes} bytes).", error={"type": "file_too_large", "actual_bytes": file_size, "max_bytes": self.max_file_size_bytes})

        try:
            original = backend.read(path)
        except Exception as e:
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message="Failed to read file.", error={"type": "read_failed", "message": str(e)})

        stale_error = check_expected_file_sha256(original, expected_file_sha256)
        if stale_error is not None:
            return _result(ok=False, applied=False, edits_would_apply=0, total_would_replace=0, edits_applied=0, total_replaced=0, edit_results=[], message=stale_error["message"], error=stale_error)

        # --- edit loop (inherited pure logic, identical to base) ---
        current = original
        edit_results: list = []
        edits_would_apply = 0
        total_would_replace = 0
        for i, edit in enumerate(edits):
            if not isinstance(edit, dict):
                return _result(ok=False, applied=False, edits_would_apply=edits_would_apply, total_would_replace=total_would_replace, edits_applied=0, total_replaced=0, edit_results=edit_results, failed_edit_index=i, message=f"Edit #{i} must be an object.", error={"type": "invalid_argument", "field": "edits"})
            if "search" not in edit or "replace" not in edit:
                return _result(ok=False, applied=False, edits_would_apply=edits_would_apply, total_would_replace=total_would_replace, edits_applied=0, total_replaced=0, edit_results=edit_results, failed_edit_index=i, message=f"Edit #{i} must include 'search' and 'replace'.", error={"type": "invalid_argument", "field": "edits"})
            if not isinstance(edit["search"], str) or edit["search"] == "":
                return _result(ok=False, applied=False, edits_would_apply=edits_would_apply, total_would_replace=total_would_replace, edits_applied=0, total_replaced=0, edit_results=edit_results, failed_edit_index=i, message=f"Edit #{i} has invalid 'search'.", error={"type": "invalid_argument", "field": "search"})
            if not isinstance(edit["replace"], str):
                return _result(ok=False, applied=False, edits_would_apply=edits_would_apply, total_would_replace=total_would_replace, edits_applied=0, total_replaced=0, edit_results=edit_results, failed_edit_index=i, message=f"Edit #{i} has invalid 'replace'.", error={"type": "invalid_argument", "field": "replace"})

            ok_edit, next_content, meta, err = self._apply_one_edit(content=current, edit=edit)
            meta["index"] = i
            edit_results.append(meta)
            if not ok_edit:
                return _result(ok=False, applied=False, edits_would_apply=edits_would_apply, total_would_replace=total_would_replace, edits_applied=0, total_replaced=0, edit_results=edit_results, failed_edit_index=i, message=f"Edit #{i} failed. No changes applied.", error=err)
            current = next_content
            edits_would_apply += 1
            total_would_replace += meta["replaced"]

        diff_text = None
        diff_truncated = False
        if return_diff:
            diff_text, diff_truncated = self._build_unified_diff(path, original, current, context_lines=diff_context_lines, max_chars=max_diff_chars)

        if dry_run:
            return _result(ok=True, applied=False, edits_would_apply=edits_would_apply, total_would_replace=total_would_replace, edits_applied=0, total_replaced=0, edit_results=edit_results, message="Dry run: batch preview generated. No changes made.", diff=diff_text, diff_truncated=diff_truncated, error=None)

        if current == original:
            return _result(ok=True, applied=False, edits_would_apply=edits_would_apply, total_would_replace=total_would_replace, edits_applied=0, total_replaced=0, edit_results=edit_results, message="No change needed after batch.", diff=diff_text, diff_truncated=diff_truncated, error=None)

        # --- backup + write (I/O swapped to container backend) ---
        backup_path = None
        if backup:
            backup_path = f"{path}.bak"
            try:
                backend.write(backup_path, original)
            except Exception as e:
                return _result(ok=False, applied=False, edits_would_apply=edits_would_apply, total_would_replace=total_would_replace, edits_applied=0, total_replaced=0, edit_results=edit_results, message="Failed to create backup file.", error={"type": "backup_failed", "message": str(e)})

        try:
            backend.write(path, current)
        except Exception as e:
            return _result(ok=False, applied=False, edits_would_apply=edits_would_apply, total_would_replace=total_would_replace, edits_applied=0, total_replaced=0, edit_results=edit_results, message="Failed to write file.", backup_path=backup_path, diff=diff_text, diff_truncated=diff_truncated, error={"type": "write_failed", "message": str(e)})

        return _result(ok=True, applied=True, edits_would_apply=edits_would_apply, total_would_replace=total_would_replace, edits_applied=edits_would_apply, total_replaced=total_would_replace, edit_results=edit_results, backup_path=backup_path, diff=diff_text, diff_truncated=diff_truncated, message="Batch edits applied successfully.", error=None)
