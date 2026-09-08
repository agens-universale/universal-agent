from gc_core.loop_detector.constants import READ_PROFILE, loop_warn_on_repeat
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


@loop_warn_on_repeat(**READ_PROFILE)
class ReadFile(AbstractSimpleTool):
    def _result(
        self,
        *,
        path: str,
        content: str,
        start_line: int,
        end_line: int,
        total_lines: int,
        truncated: bool,
        next_offset: Optional[int],
        file_size_bytes: Optional[int],
        sha256: str,
        encoding_used: Optional[str] = None,
    ) -> str:
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
            "returned_chars": len(content),
            "error": None,
            "content": content,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _error_result(
        self,
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
            "returned_chars": 0,
            "error": error_obj,
            "content": "",
        }
        if extra_fields:
            payload.update(extra_fields)
        return json.dumps(payload, ensure_ascii=False)

    def _lookup_file_size_bytes(self, path: str) -> Optional[int]:
        try:
            if self._backend.is_file(path):
                return self._backend.getsize(path)
        except OSError:
            return None
        return None

    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        description = {
            "type": "function",
            "name": "fs_read_file",
            "description": (
                "Read a text file. Optional offset (1-based, default 1) and limit "
                "(default: to end of file) select a line window. "
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
                    "encoding": {
                        "type": "string",
                        "description": (
                            "Omit for UTF-8 (UTF-8/UTF-16 BOM is detected). "
                            "Otherwise one of: utf-8-sig, cp1251, koi8-r, latin-1, "
                            "utf-16, utf-16-le, utf-16-be."
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
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        description = '''Tool name: fs_read_file
Tool description: Read a text file. Without offset/limit the line range is the whole file. Always includes full-file sha256.
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
  - "truncated": boolean — true if more lines exist after this window.
  - "next_offset": integer or null — suggested 1-based line offset for the next window.
  - "returned_chars": integer — length of "content" in this response.
  - "content": string — text of the selected line range.

Usage:
- Always use this tool to inspect the contents of any file before modifying it.
- Line range defaults to offset=1 through end of file; narrow with offset/limit when needed.
- On file_too_large: file_size_bytes is set; error.max_bytes has the limit; follow error.message.
'''
        return description


    @enforce_read_guard(path_arg="path")
    async def acall(
        self,
        path: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
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

        start_1_based = 1 if offset is None else offset
        if start_1_based < 1:
            start_1_based = 1

        if limit is not None and limit < 1:
            # Ноль (и отрицательное) — «не задан»: strict-схема заставляет слать все
            # поля, и «не использую» модель говорит нулём. Кламп до одной строки
            # отдавал ОДНУ строку как успех — модель считала файл однострочным.
            limit = None
        if limit is None:
            end_1_based = total_lines
        else:
            end_1_based = start_1_based + limit - 1

        if start_1_based > total_lines:
            start_1_based = total_lines
            end_1_based = total_lines

        if end_1_based > total_lines:
            end_1_based = total_lines

        start_idx = max(0, start_1_based - 1)
        selected_lines = lines[start_idx:end_1_based]
        content = "".join(selected_lines)

        line_truncated = end_1_based < total_lines
        next_line_offset = end_1_based + 1 if line_truncated else None

        return self._result(
            path=path,
            content=content,
            start_line=start_1_based,
            end_line=end_1_based,
            total_lines=total_lines,
            truncated=line_truncated,
            next_offset=next_line_offset,
            file_size_bytes=file_size_bytes,
            sha256=file_sha256,
            encoding_used=encoding_used,
        )
