from tools.simple_tool_base import AbstractSimpleTool
from typing import Any, Dict, Optional
import os
import json

from utils.tool_report_paths import tool_report_abspath

DEFAULT_READ_FILE_CHAR_LIMIT = 10_000
# Whole-file read is for sources/configs that fit in model context; not for huge logs.
DEFAULT_READ_FILE_MAX_BYTES = 8 * 1024 * 1024


def _default_char_limit(config: Optional[Dict[str, Any]]) -> int:
    raw = (config or {}).get("read_file_default_char_limit", DEFAULT_READ_FILE_CHAR_LIMIT)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_READ_FILE_CHAR_LIMIT
    return value if value > 0 else DEFAULT_READ_FILE_CHAR_LIMIT


def _lookup_file_size_bytes(path: str) -> Optional[int]:
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
    except OSError:
        return None
    return None


def _max_file_bytes(config: Optional[Dict[str, Any]]) -> int:
    raw = (config or {}).get("read_file_max_bytes", DEFAULT_READ_FILE_MAX_BYTES)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_READ_FILE_MAX_BYTES
    return value if value > 0 else DEFAULT_READ_FILE_MAX_BYTES


class ReadFile(AbstractSimpleTool):
    @staticmethod
    def _result(
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
    ) -> str:
        return json.dumps(
            {
                "tool": "read_file",
                "ok": True,
                "path": tool_report_abspath(path),
                "file_size_bytes": file_size_bytes,
                "start_line": start_line,
                "end_line": end_line,
                "total_lines": total_lines,
                "truncated": truncated,
                "next_offset": next_offset,
                "char_offset": char_offset,
                "char_limit": char_limit,
                "total_chars": total_chars,
                "content_truncated": content_truncated,
                "next_char_offset": next_char_offset,
                "error": None,
                "content": content,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _error_result(
        *,
        path: str,
        error_type: str,
        field: Optional[str] = None,
        message: Optional[str] = None,
        error_details: Optional[Dict[str, Any]] = None,
        file_size_bytes: Optional[int] = None,
    ) -> str:
        error_obj: Dict[str, Any] = {"type": error_type}
        if field is not None:
            error_obj["field"] = field
        if message is not None:
            error_obj["message"] = message
        if error_details:
            error_obj.update(error_details)
        return json.dumps(
            {
                "tool": "read_file",
                "ok": False,
                "path": tool_report_abspath(path),
                "file_size_bytes": file_size_bytes,
                "start_line": 0,
                "end_line": 0,
                "total_lines": 0,
                "truncated": False,
                "next_offset": None,
                "char_offset": 0,
                "char_limit": 0,
                "total_chars": 0,
                "content_truncated": False,
                "next_char_offset": None,
                "error": error_obj,
                "content": "",
            },
            ensure_ascii=False,
        )

    def _resolve_char_limit(self, char_limit: Optional[int]) -> int:
        if char_limit is None:
            return _default_char_limit(self._config_dict)
        if char_limit < 1:
            return 1
        return char_limit

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "read_file",
            "description": (
                "Read a text file from the local filesystem. "
                "Line pagination: `offset` (1-based), `limit` (max lines). "
                "Within selected lines: `char_offset` (0-based), "
                f"`char_limit` (default {DEFAULT_READ_FILE_CHAR_LIMIT}) for very long single lines."
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
                        "description": (
                            "Optional 1-based index of the first line to return. "
                            "Defaults to 1 when used together with 'limit'."
                        )
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Optional maximum number of lines to return starting from 'offset'. "
                            "If omitted but 'offset' is provided, defaults to 500. "
                            "If both 'offset' and 'limit' are omitted, a preview (first 500 lines) is returned."
                        )
                    },
                    "char_offset": {
                        "type": "integer",
                        "description": (
                            "0-based character offset into the concatenated text of the selected line range "
                            "(Unicode code points). Default 0."
                        ),
                    },
                    "char_limit": {
                        "type": "integer",
                        "description": (
                            f"Max characters to return from char_offset within the selected lines. "
                            f"Default {DEFAULT_READ_FILE_CHAR_LIMIT}."
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
        description = '''Tool name: read_file
Tool description: Reads the contents of a text file from the local filesystem with line and character pagination.
Input schema: {
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": "The path of the file to read."
    },
    "offset": {
      "type": "integer",
      "description": "1-based index of the first line to read. Use together with 'limit'."
    },
    "limit": {
      "type": "integer",
      "description": "Maximum number of lines to return starting from 'offset'. If omitted when 'offset' is provided, defaults to 500."
    },
    "char_offset": {
      "type": "integer",
      "description": "0-based character offset within the selected line range."
    },
    "char_limit": {
      "type": "integer",
      "description": "Max characters returned from char_offset (default 10000)."
    }
  },
  "required": ["path"],
  "additionalProperties": false
}

Return format:
- The tool ALWAYS returns a JSON string with the following fields (metadata first, "content" last):
  - "file_size_bytes": integer or null — on-disk file size when the path is a readable file.
  - "start_line": integer — 1-based number of the first line returned.
  - "end_line": integer — 1-based number of the last line returned.
  - "total_lines": integer — total number of lines in the file.
  - "truncated": boolean — true if more lines exist after this page.
  - "next_offset": integer or null — suggested 1-based line offset for the next page.
  - "char_offset", "char_limit": applied character slice within the selected lines.
  - "total_chars": integer — length of the selected line range before char slicing.
  - "content_truncated": boolean — true if more characters remain in that range.
  - "next_char_offset": integer or null — next char_offset when content_truncated is true.
  - "content": string — text after line and character slicing.

Usage:
- Always use this tool to inspect the contents of any file before modifying it.
- For large files, page by lines (offset/limit) and by characters (char_offset/char_limit) when content_truncated is true.
- On file_too_large: file_size_bytes is set; error.max_bytes has the limit; follow error.message.
'''
        return description

    def __call__(
        self,
        path: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        char_offset: Optional[int] = None,
        char_limit: Optional[int] = None,
        **kwargs,
    ) -> str:
        if not isinstance(path, str):
            return self._error_result(
                path=str(path),
                error_type="invalid_argument",
                field="path",
                message=f"Argument 'path' must be a string. Got: {type(path)} ({path})",
                file_size_bytes=None,
            )

        file_size_bytes = _lookup_file_size_bytes(path)

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

        if not os.path.exists(path):
            return self._error_result(
                path=path,
                error_type="not_found",
                message=f"File not found: {path}",
                file_size_bytes=None,
            )

        if os.path.isdir(path):
            return self._error_result(
                path=path,
                error_type="not_a_file",
                message=f"Cannot read file: '{path}' is a directory.",
                file_size_bytes=None,
            )

        max_bytes = _max_file_bytes(self._config_dict)
        if file_size_bytes is None and os.path.exists(path):
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
                    f"File is {file_size_bytes} bytes; read_file limit is {max_bytes} bytes. "
                    "This tool loads the whole file for in-context inspection — too large for that. "
                    "Choose a different approach from <tools> matched to your goal: read a small slice, "
                    "tail/head, search for patterns, or compute a summary — not a full read through this tool."
                ),
                error_details={"max_bytes": max_bytes},
                file_size_bytes=file_size_bytes,
            )

        resolved_char_limit = self._resolve_char_limit(char_limit)
        resolved_char_offset = 0 if char_offset is None else char_offset

        with open(path, 'r', encoding='utf-8') as fd:
            lines = fd.readlines()

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
        )
