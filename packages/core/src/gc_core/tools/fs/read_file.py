from gc_core.tools.simple_tool_base import AbstractSimpleTool
from typing import Any, Dict, Optional
import json

from gc_core.utils.tool_report_paths import tool_report_abspath
from gc_core.security.read_guard import enforce_read_guard
from gc_core.tools.schema_optional import coalesce_optional_string
from gc_core.utils.file_text import decode_text_bytes, sha256_hex_of_bytes
from gc_core.utils.fs_size_limits import (
    DEFAULT_FS_READ_MAX_BYTES as DEFAULT_READ_FILE_MAX_BYTES,  # noqa: F401  # re-export для тестов
    check_file_size,
    fs_read_max_bytes,
)

# Safety cap for one char-page of the selected line range. Normal sources/configs fit;
# oversized blobs page via char_offset. Model-facing preview still goes through stored_tool_output.
DEFAULT_READ_FILE_CHAR_LIMIT = 100_000


def _default_char_limit(config: Optional[Dict[str, Any]]) -> int:
    raw = (config or {}).get("read_file_default_char_limit", DEFAULT_READ_FILE_CHAR_LIMIT)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_READ_FILE_CHAR_LIMIT
    return value if value > 0 else DEFAULT_READ_FILE_CHAR_LIMIT


class ReadFile(AbstractSimpleTool):
    def _result(self, 
        *,
        path: str,
        content: str,
        start_line: int,
        end_line: int,
        total_lines: int,
        truncated: bool,
        next_offset: Optional[int],
        char_offset: int,
        char_limit: int,
        total_chars: int,
        content_truncated: bool,
        next_char_offset: Optional[int],
        file_size_bytes: Optional[int],
        sha256: str,
        hint: Optional[str] = None,
        encoding_used: Optional[str] = None,
    ) -> str:
        returned_chars = len(content)
        payload: Dict[str, Any] = {
            "tool": ReadFile.registered_tool_name(),
            "ok": True,
            "path": tool_report_abspath(path, workspace=self._workspace),
            "sha256": sha256,
            "encoding_used": encoding_used,
            "file_size_bytes": file_size_bytes,
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": total_lines,
            "truncated": truncated,
            "next_offset": next_offset,
            "char_offset": char_offset,
            "char_limit": char_limit,
            "total_chars": total_chars,
            "returned_chars": returned_chars,
            "content_truncated": content_truncated,
            "next_char_offset": next_char_offset,
            "error": None,
            "content": content,
        }
        if hint is not None:
            payload["hint"] = hint
        return json.dumps(payload, ensure_ascii=False)

    def _error_result(self, 
        *,
        path: str,
        error_type: str,
        field: Optional[str] = None,
        message: Optional[str] = None,
        error_details: Optional[Dict[str, Any]] = None,
        file_size_bytes: Optional[int] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> str:
        error_obj: Dict[str, Any] = {"type": error_type}
        if field is not None:
            error_obj["field"] = field
        if message is not None:
            error_obj["message"] = message
        if error_details:
            error_obj.update(error_details)
        payload: Dict[str, Any] = {
            "tool": ReadFile.registered_tool_name(),
            "ok": False,
            "path": tool_report_abspath(path, workspace=self._workspace),
            "sha256": None,
            "encoding_used": None,
            "file_size_bytes": file_size_bytes,
            "start_line": 0,
            "end_line": 0,
            "total_lines": 0,
            "truncated": False,
            "next_offset": None,
            "char_offset": 0,
            "char_limit": 0,
            "total_chars": 0,
            "returned_chars": 0,
            "content_truncated": False,
            "next_char_offset": None,
            "error": error_obj,
            "content": "",
        }
        if extra_fields:
            payload.update(extra_fields)
        return json.dumps(payload, ensure_ascii=False)

    def _resolve_char_limit(self, char_limit: Optional[int]) -> int:
        if char_limit is None:
            return _default_char_limit(self._config_dict)
        if char_limit < 1:
            return 1
        return char_limit

    def _lookup_file_size_bytes(self, path: str) -> Optional[int]:
        try:
            if self._backend.is_file(path):
                return self._backend.getsize(path)
        except OSError:
            return None
        return None

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "fs_read_file",
            "description": (
                "Read a text file with line and character pagination. "
                "Without offset/limit the line range is the whole file; page long content with "
                "char_offset/char_limit when content_truncated is true (continue from next_char_offset). "
                "Always includes full-file sha256 (for expected_file_sha256 in edit tools)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path of the file to read. Must be a valid file path and not a directory."
                    },
                    "offset": {
                        "type": "integer",
                        "description": "1-based first line of the range to read. Default 1.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Max lines to read starting from offset. "
                            "Default: to end of file."
                        ),
                    },
                    "char_offset": {
                        "type": "integer",
                        "description": (
                            "0-based character offset within the line range. Default 0. "
                            "When content_truncated is true, continue with next_char_offset."
                        ),
                    },
                    "char_limit": {
                        "type": "integer",
                        "description": (
                            f"Max characters to return from char_offset. "
                            f"Default {DEFAULT_READ_FILE_CHAR_LIMIT}."
                        ),
                    },
                    "encoding": {
                        "type": "string",
                        "description": (
                            "Optional text encoding. Omit for UTF-8 (default; also UTF-16 with BOM). "
                            "Otherwise: utf-8, utf-8-sig, cp1251, koi8-r, latin-1, utf-16, "
                            "utf-16-le, utf-16-be (also windows-1251, iso-8859-1)."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False
            },
            "strict": True
        }
        return description

    @classmethod
    def prompt_description(cls):
        description = '''Tool name: fs_read_file
Tool description: Read a text file with line and character pagination. Without offset/limit the line range is the whole file; page with char_offset/char_limit when content_truncated (continue from next_char_offset). Always includes full-file sha256.
Input schema: {
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": "The path of the file to read."
    },
    "offset": {
      "type": "integer",
      "description": "1-based first line of the range to read. Default 1."
    },
    "limit": {
      "type": "integer",
      "description": "Max lines to read starting from offset. Default: to end of file."
    },
    "char_offset": {
      "type": "integer",
      "description": "0-based character offset within the line range. Default 0. When content_truncated is true, continue with next_char_offset."
    },
    "char_limit": {
      "type": "integer",
      "description": "Max characters to return from char_offset. Default 100000."
    }
  },
  "required": ["path"],
  "additionalProperties": false
}

Return format:
- The tool ALWAYS returns a JSON string with the following fields (metadata first, "content" last):
  - "sha256": string — SHA-256 hex digest of the full file (same canonical read as edit tools / file_info).
  - "file_size_bytes": integer or null — on-disk file size when the path is a readable file.
  - "start_line": integer — 1-based number of the first line returned.
  - "end_line": integer — 1-based number of the last line returned.
  - "total_lines": integer — total number of lines in the file.
  - "truncated": boolean — true if more lines exist after this page.
  - "next_offset": integer or null — suggested 1-based line offset for the next page.
  - "char_offset", "char_limit": applied character slice within the line range.
  - "total_chars": integer — length of the current line range (not always the whole file).
  - "returned_chars": integer — length of "content" in this response.
  - "content_truncated": boolean — true if more characters remain in that range.
  - "next_char_offset": integer or null — next char_offset when content_truncated is true.
  - "hint": string (optional) — only when content was truncated; how much was returned vs total and how to continue.
  - "content": string — text after line and character slicing.

Usage:
- Always use this tool to inspect the contents of any file before modifying it.
- Line range defaults to offset=1 through end of file; narrow with offset/limit when needed.
- Page long ranges with char_offset/char_limit when content_truncated is true.
- On file_too_large: file_size_bytes is set; error.max_bytes has the limit; follow error.message.
'''
        return description

    async def acall(self, *args, **kwargs) -> str:
        """Инлайн на loop: нативного async у файлов нет (docs/async.md)."""
        return self(*args, **kwargs)

    @enforce_read_guard(path_arg="path")
    def __call__(
        self,
        path: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        char_offset: Optional[int] = None,
        char_limit: Optional[int] = None,
        encoding: Optional[str] = None,
        **kwargs,
    ) -> str:
        encoding = coalesce_optional_string(encoding)
        if not isinstance(path, str):
            return self._error_result(
                path=str(path),
                error_type="invalid_argument",
                field="path",
                message=f"Argument 'path' must be a string. Got: {type(path)} ({path})",
                file_size_bytes=None,
            )

        file_size_bytes = self._lookup_file_size_bytes(path)

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

        if not self._backend.exists(path):
            return self._error_result(
                path=path,
                error_type="not_found",
                message=f"File not found: {path}",
                file_size_bytes=None,
            )

        if self._backend.is_dir(path):
            return self._error_result(
                path=path,
                error_type="not_a_file",
                message=f"Cannot read file: '{path}' is a directory.",
                file_size_bytes=None,
            )

        max_bytes = fs_read_max_bytes(self._config_dict)
        if file_size_bytes is None and self._backend.exists(path):
            return self._error_result(
                path=path,
                error_type="io_error",
                message="Cannot determine file size.",
                file_size_bytes=None,
            )
        if file_size_bytes is not None:
            size_err = check_file_size(
                file_size_bytes, max_bytes, operation="fs_read_file"
            )
            if size_err is not None:
                return self._error_result(
                    path=path,
                    error_type="file_too_large",
                    message=(
                        f"{size_err['message']} "
                        "Choose a different approach from <tools> matched to your goal: "
                        "read a small slice, tail/head, search for patterns, or compute a summary — "
                        "not a full read through this tool. "
                        "Raise fs_tools.max_read_bytes in config if needed."
                    ),
                    error_details={"max_bytes": max_bytes, "actual_bytes": file_size_bytes},
                    file_size_bytes=file_size_bytes,
                )

        resolved_char_limit = self._resolve_char_limit(char_limit)
        resolved_char_offset = 0 if char_offset is None else char_offset

        try:
            raw = self._backend.read_bytes(path)
        except OSError as e:
            return self._error_result(
                path=path,
                error_type="read_failed",
                message=f"Failed to read file: {e}",
                file_size_bytes=file_size_bytes,
            )
        decoded = decode_text_bytes(raw, encoding=encoding, include_preview=True)
        if not decoded.ok:
            err = decoded.error or {"type": "read_failed", "message": "Failed to decode file."}
            extra = None
            if err.get("type") == "encoding_required":
                extra = {
                    "encoding_guess": err.get("encoding_guess"),
                    "encoding_confidence": err.get("encoding_confidence"),
                    "preview": err.get("preview"),
                    "preview_encoding": err.get("preview_encoding"),
                    "preview_truncated": err.get("preview_truncated"),
                }
                # Keep error compact: type + message (+ guess fields for agents).
                err = {
                    "type": "encoding_required",
                    "message": err.get("message"),
                    "encoding_guess": err.get("encoding_guess"),
                    "encoding_confidence": err.get("encoding_confidence"),
                }
            return self._error_result(
                path=path,
                error_type=err["type"],
                message=err.get("message"),
                error_details={
                    k: v for k, v in err.items() if k not in ("type", "message")
                } or None,
                file_size_bytes=file_size_bytes,
                extra_fields=extra,
            )
        full_text = decoded.text or ""
        file_sha256 = sha256_hex_of_bytes(raw)
        encoding_used = decoded.encoding_used
        lines = full_text.splitlines(keepends=True)
        total_lines = len(lines)

        # Line range: offset default 1, limit default = rest of file.
        start_1_based = 1 if offset is None else offset
        if start_1_based < 1:
            start_1_based = 1

        if limit is None:
            end_1_based = total_lines
        else:
            if limit < 1:
                limit = 1
            end_1_based = start_1_based + limit - 1

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
        returned_chars = len(content)
        remaining_chars = total_chars - char_end
        hint = None
        if content_truncated:
            hint = (
                f"Character page truncated: returned {returned_chars} of {total_chars} chars "
                f"in the selected line range ({remaining_chars} remain after this page). "
                f"Continue with the same path/offset/limit and char_offset={next_char_offset}."
            )

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
            encoding_used=encoding_used,
            hint=hint,
        )
