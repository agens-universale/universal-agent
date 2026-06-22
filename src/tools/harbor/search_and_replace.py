"""Harbor variant of fs_search_and_replace.

Faithful 1:1 reimplementation of ``SearchAndReplace.__call__`` for the External adapter:
same validation, same occurrence/scope logic (inherited pure helpers), same ``_result``
envelope. The ONLY difference is physical I/O — file existence/size/read/write go through
HarborBackend (the task container) instead of the host filesystem. The base
``@enforce_write_guard`` decorator is intentionally dropped: the container is the sandbox.
"""
import functools

from tools.fs.search_and_replace import SearchAndReplace
from tools.schema_optional import coalesce_optional_string
from utils.file_text import check_expected_file_sha256, is_valid_sha256_hex


class HarborSearchAndReplace(SearchAndReplace):
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
        diff_context_lines: int = SearchAndReplace.default_diff_context_lines,
        max_diff_chars: int = SearchAndReplace.default_max_diff_chars,
        expected_file_sha256: str | None = None,
        **kwargs,
    ) -> str:
        backend = self._ctx.harbor_backend
        expected_file_sha256 = coalesce_optional_string(expected_file_sha256)
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
        if not backend.exists(path):
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
        if not backend.is_file(path):
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
            file_size = backend.getsize(path)
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
            content = backend.read(path)
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

        backup_path = None
        if backup:
            backup_path = f"{path}.bak"
            try:
                backend.write(backup_path, content)
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
            backend.write(path, new_content)
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
