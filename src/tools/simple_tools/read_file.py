from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool
from typing import Optional
import os


class ReadFile(AbstractSimpleTool):
    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "read_file",
            "description": "Reads the contents of a file from the local filesystem. Always use this tool to inspect a file before modifying it. If 'view_range' is not provided, only the first 500 lines are returned with a note indicating the total number of lines in the file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path of the file to read. Must be a valid file path and not a directory."
                    },
                    "view_range": {
                        "type": "string",
                        "description": "Optional range of lines to read, formatted as 'start_line,end_line' (inclusive). For example, '7,20' returns lines 7 through 20. If omitted, the entire file is read up to the first 500 lines."
                    }
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
Tool description: Reads the contents of a file from the local filesystem.
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "The path of the file to read."}, "view_range": {"type": "string", "description": "A range of lines to show, e.g., \\"7,20\\"."}}, "required": ["path"], "additionalProperties": false}

Usage:
- Always use this tool to inspect the contents of any file before modifying it.
- If you need to understand or modify an existing file, read it first using this tool.
- Use the 'view_range' parameter to read only specific lines if needed (e.g., "10,20" for lines 10 to 20 inclusive).
- The 'view_range' should be provided as two numbers separated by a comma (start_line,end_line). Both are inclusive.
- If 'view_range' is not provided, the entire file will be read. Long files will be cut to first 500 rows and suffix <Lines in file {num_lines}, displayed first 500 lines>
- This tool is essential for avoiding mistakes when editing code. Never assume the content of a file — always read it first.
'''
        return description

    def __call__(self, path: str, view_range: Optional[str] = None, **kwargs) -> str:
        if not isinstance(path, str):
            return f"Error: Argument 'path' must be a string. Got: {type(path)} ({path})"

        if view_range is not None and not isinstance(view_range, str):
            return f"Error: Argument 'view_range' must be a string or None. Got: {type(view_range)} ({view_range})"

        if not os.path.exists(path):
            return f"Error: File not found: {path}"

        if os.path.isdir(path):
            return f"Error: Cannot read file: '{path}' is a directory."

        with open(path, 'r', encoding='utf-8') as fd:
            lines = fd.readlines()

        initial_view_range = view_range
        if view_range is None:
            view_range = '0,500'

        parts = view_range.strip().split(',')
        if len(parts) != 2:
            raise ValueError("view_range must have exactly two values: start,end")

        start_line = int(parts[0]) - 1  # Convert to zero-based index
        end_line = int(parts[1])       # End is inclusive, so we slice to end_line

        if start_line < 0:
            start_line = 0
        if end_line > len(lines):
            end_line = len(lines)

        selected_lines = lines[start_line:end_line]

        res = ''.join(selected_lines)

        if len(lines) > end_line and initial_view_range is None:
            res += f'<Lines in file {len(lines)}, displayed first 500 lines>'
        return res


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
