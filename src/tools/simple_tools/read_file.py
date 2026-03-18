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
                "path": path,
                "content": content,
                "start_line": start_line,
                "end_line": end_line,
                "total_lines": total_lines,
                "truncated": truncated,
                "next_offset": next_offset,
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
                    "view_range": {
                        "type": "string",
                        "description": (
                            "Optional range of lines to read, formatted as 'start_line,end_line' (inclusive, 1-based). "
                            "For example, '7,20' returns lines 7 through 20. "
                            "If omitted and 'offset'/'limit' are not provided, a preview (first 500 lines) is returned. "
                            "This parameter is kept for backward compatibility; prefer using 'offset' and 'limit'."
                        )
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
    "view_range": {
      "type": "string",
      "description": "DEPRECATED. A range of lines to show, e.g., \\"7,20\\" (1-based, inclusive). Kept for backward compatibility; prefer 'offset' and 'limit'."
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
- The older 'view_range' parameter is supported but should generally be avoided in new code; use 'offset' and 'limit' instead.
'''
        return description

    def __call__(
        self,
        path: str,
        view_range: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        **kwargs,
    ) -> str:
        if not isinstance(path, str):
            return f"Error: Argument 'path' must be a string. Got: {type(path)} ({path})"

        if view_range is not None and not isinstance(view_range, str):
            return f"Error: Argument 'view_range' must be a string or None. Got: {type(view_range)} ({view_range})"

        if offset is not None and not isinstance(offset, int):
            return f"Error: Argument 'offset' must be an integer or None. Got: {type(offset)} ({offset})"

        if limit is not None and not isinstance(limit, int):
            return f"Error: Argument 'limit' must be an integer or None. Got: {type(limit)} ({limit})"

        if not os.path.exists(path):
            return f"Error: File not found: {path}"

        if os.path.isdir(path):
            return f"Error: Cannot read file: '{path}' is a directory."

        with open(path, 'r', encoding='utf-8') as fd:
            lines = fd.readlines()

        total_lines = len(lines)

        # Determine the range to read.
        # Priority:
        #  1) Explicit view_range (backward compatibility, 1-based inclusive)
        #  2) offset/limit (preferred)
        #  3) Default preview: first 500 lines
        if view_range is not None:
            parts = view_range.strip().split(',')
            if len(parts) != 2:
                return "Error: view_range must have exactly two comma-separated values: start,end"
            try:
                start_1_based = int(parts[0])
                end_1_based = int(parts[1])
            except ValueError:
                return "Error: view_range values must be integers, e.g. '7,20'."

            if start_1_based < 1:
                start_1_based = 1
            if end_1_based < start_1_based:
                end_1_based = start_1_based

            if start_1_based > total_lines:
                start_1_based = total_lines
                end_1_based = total_lines

        elif offset is not None or limit is not None:
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


def test_read_file():
    # Write test file
    test_content = "\n".join([f"Line {i+1}: Lorem ipsum {i}" for i in range(20)])
    test_path = "./test_read_file.txt"

    long_content = "\n".join([f"Line {i+1}: Lorem ipsum {i}" for i in range(2000)])
    long_file_path = "./test_read_long_file.txt"

    with open(test_path, 'w', encoding='utf-8') as f:
        f.write(test_content)

    with open(long_file_path, 'w', encoding='utf-8') as f:
        f.write(long_content)

    reader = ReadFile()

    # Test long file read
    content_read = reader(path=long_file_path)
    content_cut = "\n".join([f"Line {i+1}: Lorem ipsum {i}" for i in range(500)]) + f'\n<Lines in file 2000, displayed first 500 lines>'
    assert content_read.strip() == content_cut.strip()

    # Test full read
    content_full = reader(path=test_path)
    assert content_full.strip() == test_content.strip()

    # Test range read
    content_part = reader(path=test_path, view_range="5,10")
    expected_part = "\n".join([f"Line {i+1}: Lorem ipsum {i}" for i in range(4, 10)]) + "\n"  # Lines 5–10
    assert content_part.strip() == expected_part.strip()

    # Test edge: out of bounds
    content_large_range = reader(path=test_path, view_range="15,30")
    expected_large = "\n".join([f"Line {i+1}: Lorem ipsum {i}" for i in range(14, 20)])
    assert content_large_range.strip() == expected_large.strip()

    # Cleanup
    os.remove(test_path)
    os.remove(long_file_path)
    print("All tests passed.")

if __name__ == '__main__':
    test_read_file()
