"""Harbor variant of fs_read_file.

Faithful 1:1 reimplementation of ``ReadFile.__call__`` for the External adapter:
same validation, same pagination/sha256 logic, same ``_result``/``_error_result``
envelope. The ONLY difference is physical I/O — file existence/size/read go through
HarborBackend (the task container) instead of the host filesystem. The base
``@enforce_read_guard`` decorator is intentionally dropped: the container is the sandbox.
"""
from typing import Optional

from tools.fs.read_file import ReadFile, _default_char_limit, _max_file_bytes
from utils.file_text import sha256_hex_of_text


class HarborReadFile(ReadFile):
    def __call__(
        self,
        path: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        char_offset: Optional[int] = None,
        char_limit: Optional[int] = None,
        **kwargs,
    ) -> str:
        backend = self._ctx.harbor_backend
        config_dict = getattr(self._ctx, "config_dict", None)

        if not isinstance(path, str):
            return self._error_result(
                path=str(path),
                error_type="invalid_argument",
                field="path",
                message=f"Argument 'path' must be a string. Got: {type(path)} ({path})",
                file_size_bytes=None,
            )

        # File size via backend (guard with try to mirror base behavior on failure)
        try:
            file_size_bytes = backend.getsize(path) if backend.is_file(path) else None
        except OSError:
            file_size_bytes = None

        if offset is not None and not isinstance(offset, int):
            return self._error_result(
                path=path,
                error_type="invalid_argument",
                field="offset",
                message=f"Argument 'offset' must be an integer or None. Got: {type(offset)} ({offset})",
                file_size_bytes=file_size_bytes,
            )

        if limit is not None and not isinstance(limit, int):
            return self._error_result(
                path=path,
                error_type="invalid_argument",
                field="limit",
                message=f"Argument 'limit' must be an integer or None. Got: {type(limit)} ({limit})",
                file_size_bytes=file_size_bytes,
            )

        if char_offset is not None and not isinstance(char_offset, int):
            return self._error_result(
                path=path,
                error_type="invalid_argument",
                field="char_offset",
                message=(
                    f"Argument 'char_offset' must be an integer or None. "
                    f"Got: {type(char_offset)} ({char_offset})"
                ),
                file_size_bytes=file_size_bytes,
            )

        if char_limit is not None and not isinstance(char_limit, int):
            return self._error_result(
                path=path,
                error_type="invalid_argument",
                field="char_limit",
                message=(
                    f"Argument 'char_limit' must be an integer or None. "
                    f"Got: {type(char_limit)} ({char_limit})"
                ),
                file_size_bytes=file_size_bytes,
            )

        if char_limit is not None and char_limit < 1:
            return self._error_result(
                path=path,
                error_type="invalid_argument",
                field="char_limit",
                message="Argument 'char_limit' must be >= 1.",
                file_size_bytes=file_size_bytes,
            )

        if char_offset is not None and char_offset < 0:
            return self._error_result(
                path=path,
                error_type="invalid_argument",
                field="char_offset",
                message="Argument 'char_offset' must be >= 0.",
                file_size_bytes=file_size_bytes,
            )

        if not backend.exists(path):
            return self._error_result(
                path=path,
                error_type="not_found",
                message=f"File not found: {path}",
                file_size_bytes=None,
            )

        if backend.is_dir(path):
            return self._error_result(
                path=path,
                error_type="not_a_file",
                message=f"Cannot read file: '{path}' is a directory.",
                file_size_bytes=None,
            )

        max_bytes = _max_file_bytes(config_dict)
        if file_size_bytes is None and backend.exists(path):
            return self._error_result(
                path=path,
                error_type="io_error",
                message="Cannot determine file size.",
                file_size_bytes=None,
            )
        if file_size_bytes is not None and file_size_bytes > max_bytes:
            return self._error_result(
                path=path,
                error_type="file_too_large",
                message=(
                    f"File is {file_size_bytes} bytes; fs_read_file limit is {max_bytes} bytes. "
                    "This tool loads the whole file for in-context inspection — too large for that. "
                    "Choose a different approach from <tools> matched to your goal: read a small slice, "
                    "tail/head, search for patterns, or compute a summary — not a full read through this tool."
                ),
                error_details={"max_bytes": max_bytes},
                file_size_bytes=file_size_bytes,
            )

        resolved_char_limit = _default_char_limit(config_dict) if char_limit is None else max(char_limit, 1)
        resolved_char_offset = 0 if char_offset is None else char_offset

        try:
            full_text = backend.read(path)
        except OSError as e:
            return self._error_result(
                path=path,
                error_type="read_failed",
                message=f"Failed to read file: {e}",
                file_size_bytes=file_size_bytes,
            )

        file_sha256 = sha256_hex_of_text(full_text)
        lines = full_text.splitlines(keepends=True)
        total_lines = len(lines)

        if offset is not None or limit is not None:
            if offset is None:
                offset = 1
            if limit is None:
                limit = 500

            if offset < 1:
                offset = 1
            if limit < 1:
                limit = 1

            start_1_based = offset
            end_1_based = offset + limit - 1
        else:
            start_1_based = 1
            end_1_based = 500

        if start_1_based > total_lines:
            start_1_based = total_lines
            end_1_based = total_lines

        if end_1_based > total_lines:
            end_1_based = total_lines

        start_idx = max(0, start_1_based - 1)
        end_idx_exclusive = end_1_based

        selected_lines = lines[start_idx:end_idx_exclusive]
        line_chunk = ''.join(selected_lines)
        total_chars = len(line_chunk)

        safe_char_offset = min(resolved_char_offset, total_chars)
        char_end = min(safe_char_offset + resolved_char_limit, total_chars)
        content = line_chunk[safe_char_offset:char_end]

        line_truncated = end_1_based < total_lines
        next_line_offset = end_1_based + 1 if line_truncated else None

        content_truncated = char_end < total_chars
        next_char_offset = char_end if content_truncated else None

        return self._result(
            path=path,
            content=content,
            start_line=start_1_based,
            end_line=end_1_based,
            total_lines=total_lines,
            truncated=line_truncated,
            next_offset=next_line_offset,
            char_offset=safe_char_offset,
            char_limit=resolved_char_limit,
            total_chars=total_chars,
            content_truncated=content_truncated,
            next_char_offset=next_char_offset,
            file_size_bytes=file_size_bytes,
            sha256=file_sha256,
        )
