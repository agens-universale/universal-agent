from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool
from typing import Optional
import os
import json


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
    ) -> str:
        return json.dumps(
            {
                "tool": "read_file",
                "ok": True,
                "path": path,
                "content": content,
                "start_line": start_line,
                "end_line": end_line,
                "total_lines": total_lines,
                "truncated": truncated,
                "next_offset": next_offset,
                "error": None,
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
    ) -> str:
        error_obj = {"type": error_type}
        if field is not None:
            error_obj["field"] = field
        if message is not None:
            error_obj["message"] = message
        return json.dumps(
            {
                "tool": "read_file",
                "ok": False,
                "path": path,
                "content": "",
                "start_line": 0,
                "end_line": 0,
                "total_lines": 0,
                "truncated": False,
                "next_offset": None,
                "error": error_obj,
            },
            ensure_ascii=False,
        )

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "read_file",
            "description": (
                "Reads the contents of a text file from the local filesystem with optional pagination. "
                "By default, returns a preview (first 500 lines) together with metadata about the full file "
                "so the caller can request additional ranges as needed."
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
Tool description: Reads the contents of a text file from the local filesystem with explicit pagination support.
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
    }
  },
  "required": ["path"],
  "additionalProperties": false
}

Return format:
- The tool ALWAYS returns a JSON string with the following fields:
  - "content": string — concatenated file lines in the requested range.
  - "start_line": integer — 1-based number of the first line returned.
  - "end_line": integer — 1-based number of the last line returned.
  - "total_lines": integer — total number of lines in the file.
  - "truncated": boolean — true if not all lines of the file are included in "content".
  - "next_offset": integer or null — suggested 1-based offset for the next page (only set when truncated is true).

Usage:
- Always use this tool to inspect the contents of any file before modifying it.
- If you need to understand or modify an existing file, read it first using this tool.
- For small files (<= 500 lines), a single call without 'offset'/'limit' is usually enough.
- For large files, you MUST use pagination when you need the full content:
  1) First, call read_file with only "path" to get a preview and metadata.
  2) If "truncated" is true, call read_file again with "offset" = "next_offset" and an appropriate "limit" (e.g., 500).
  3) Repeat until "truncated" becomes false.
- When copying, restoring, or refactoring large files, ALWAYS read the entire file by paging through ranges using "offset" and "limit" before writing changes back.
'''
        return description

    def __call__(
        self,
        path: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        **kwargs,
    ) -> str:
        if not isinstance(path, str):
            return self._error_result(
                path=str(path),
                error_type="invalid_argument",
                field="path",
                message=f"Argument 'path' must be a string. Got: {type(path)} ({path})",
            )

        if offset is not None and not isinstance(offset, int):
            return self._error_result(
                path=path,
                error_type="invalid_argument",
                field="offset",
                message=f"Argument 'offset' must be an integer or None. Got: {type(offset)} ({offset})",
            )

        if limit is not None and not isinstance(limit, int):
            return self._error_result(
                path=path,
                error_type="invalid_argument",
                field="limit",
                message=f"Argument 'limit' must be an integer or None. Got: {type(limit)} ({limit})",
            )

        if not os.path.exists(path):
            return self._error_result(
                path=path,
                error_type="not_found",
                message=f"File not found: {path}",
            )

        if os.path.isdir(path):
            return self._error_result(
                path=path,
                error_type="not_a_file",
                message=f"Cannot read file: '{path}' is a directory.",
            )

        with open(path, 'r', encoding='utf-8') as fd:
            lines = fd.readlines()

        total_lines = len(lines)

        # Determine the range to read.
        # Priority:
        #  1) offset/limit (preferred)
        #  2) Default preview: first 500 lines
        if offset is not None or limit is not None:
            # Use offset/limit pagination.
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
            # Default preview: first 500 lines
            start_1_based = 1
            end_1_based = 500

        if start_1_based > total_lines:
            # Empty range: no content, clamp to end of file
            start_1_based = total_lines
            end_1_based = total_lines

        if end_1_based > total_lines:
            end_1_based = total_lines

        start_idx = max(0, start_1_based - 1)
        end_idx_exclusive = end_1_based  # slice end is exclusive, lines are 0-based

        selected_lines = lines[start_idx:end_idx_exclusive]
        content = ''.join(selected_lines)

        truncated = end_1_based < total_lines
        next_offset = end_1_based + 1 if truncated else None

        return self._result(
            path=path,
            content=content,
            start_line=start_1_based,
            end_line=end_1_based,
            total_lines=total_lines,
            truncated=truncated,
            next_offset=next_offset,
        )

