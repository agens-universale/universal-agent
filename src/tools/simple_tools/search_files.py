import json
import functools
import os
import re
import shutil
from typing import Optional, List, Dict, Any
from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool
from utils.glob_to_regex import compile_glob


class SearchFiles(AbstractSimpleTool):
    @classmethod
    def tool_description(cls):
        return {
            "type": "function",
            "name": "search_files",
            "description": (
                "Recursively searches for a regex pattern in file contents under a directory, optionally filtered "
                "by a glob pattern. Returns a JSON string object with fields: ok, path, regex, file_pattern, "
                "count, matches, error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The directory to search in. Must be a valid absolute path."
                    },
                    "regex": {
                        "type": "string",
                        "description": "The Rust-style regular expression pattern to search for. Must be a valid pattern without unsupported features like backreferences or variable-length lookarounds. Use double backslashes (e.g., \\d, \\s)."
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Optional glob pattern to filter files (e.g., '*.py', 'src/**/*.rs'). If not provided, all files are searched."
                    }
                },
                "required": ["path", "regex"],
                "additionalProperties": False
            },
            "strict": True
        }

    @classmethod
    def prompt_description(cls):
        description = '''Tool name: search_files  
Tool description: Recursively searches for a regex pattern in all files within a specified directory, optionally filtered by a glob pattern.
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "The directory to search in."}, "regex": {"type": "string", "description": "The Rust-style regex pattern to search for."}, "file_pattern": {"type": "string", "description": "Glob pattern to filter files (e.g., \'*.py\')."}}, "required": ["path", "regex"], "additionalProperties": false}

Usage:
- Searches **all files** recursively under the given directory.
- Filters files using a **glob pattern** (e.g., `"*.py"`, `"src/**/*.rs"`).
- Matches lines using a **Rust-style regular expression**.
- Returns a list of matches with file paths and line contents.
- Returns an empty list if no matches are found.

Important Usage Rules
- **ALWAYS** use `search_files` for searching text or code.
- **NEVER** use external tools like `grep`, `rg`, or shell commands — this tool is optimized and permission-safe.
- Always provide a **full absolute path** for the `path` parameter.
- The `regex` must be a valid Rust-compatible pattern. Avoid unsafe features:
  - No backreferences (e.g., `\1`, `\2`)
  - No variable-length lookarounds (e.g., `(?<=a.*)b`)
  - No recursive or deeply nested patterns
- **No multiline matching**: patterns must match within a **single line**.
- Use **simple, efficient regex patterns** to avoid performance issues or excessive results.
- Always escape special sequences with **double backslashes**: use `\\d`, `\\s`, `\\w`, `\\.` instead of `\d`, `\s`, etc.
- Use `file_pattern` to narrow scope (e.g., `"*.ts"`, `"tests/**/*"`) when possible.

Supported Glob Patterns in `file_pattern`
- `*` — Matches any sequence of characters except `/`.
- `**` — Matches any sequence, including `/` (recursive across directories).
- `?` — Matches any single character except `/`.
- `[seq]` — Matches any one character in `seq` (e.g., `[abc]`).
- `[!seq]` — Matches any character **not** in `seq` (e.g., `[!0-9]`).

Examples
Find all function definitions in Python files:
{
 "path": "/home/user/project",
 "regex": "def\\s+\\w+\\s*\\(",
 "file_pattern": "*.py"
}
   
Search for error logs in Rust source files:
{
  "path": "/home/user/project/src",
  "regex": "log.*Error",
  "file_pattern": "**/*.rs"
}
'''
        return description

    def _extract_literal(self, pattern: str) -> Optional[str]:
        """Пытается найти буквенный фрагмент в регулярке для быстрой фильтрации."""
        match = re.search(r'[a-zA-Z_а-яА-Я][a-zA-Z0-9_а-яА-Я]+', pattern)
        return match.group(0) if match else None

    def _search_in_file(self, file_path: str, regex: str, context_num=5) -> List[Dict[str, Any]]:
        try:
            compiled_regex = re.compile(regex)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern '{regex}': {str(e)}")

        literal_hint = self._extract_literal(regex)

        results = []
        buffer = []
        found_data = None
        max_line_length = 1000
        line_context_window = 500

        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line_num, line in enumerate(f, start=1):
                    line_cutted = line[:max_line_length] + f'...[line_cutted. Show first {max_line_length}, cut last {len(line) - max_line_length}]\n' if len(line) > max_line_length else line
                    buffer.append(line_cutted)
                    if found_data is not None:
                        found_data['context'] = ''.join(buffer)
                        results.append(found_data)
                        found_data = None

                    if literal_hint and literal_hint not in line:
                        buffer = buffer[-context_num:]
                        # print(f'literal_hint: {literal_hint} - skip: ', line)
                        continue

                    res = compiled_regex.search(line)
                    if res:
                        span = res.span()
                        left = max(0,span[0] - line_context_window)
                        right = min(len(line), span[1] + line_context_window)
                        found_content = line[left:right]
                        if left > 0 :
                            found_content = f'[...cutted first {left}]' + found_content
                        if right < len(line):
                            found_content += f'[cutted last {len(line) - right}...]'
                        if left > 0 or right < len(line):
                            found_content += f'(shown from {left} to {right} symbols)'
                        else:
                            found_content = found_content.strip()
                        found_data = {
                            "file": file_path,
                            "line": line_num,
                            "line_content": found_content
                        }
                    buffer = buffer[-context_num:]
                if found_data is not None:
                    found_data['context'] = ''.join(buffer)
                    results.append(found_data)
                    found_data = None
        except Exception:
            pass

        return results

    @staticmethod
    def _result(
        *,
        path: str,
        regex: str,
        file_pattern: Optional[str],
        ok: bool,
        matches: Optional[List[Dict[str, Any]]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> str:
        return json.dumps(
            {
                "tool": "search_files",
                "ok": ok,
                "path": path,
                "regex": regex,
                "file_pattern": file_pattern,
                "count": len(matches) if matches is not None else 0,
                "matches": matches if matches is not None else [],
                "error": error,
            },
            ensure_ascii=False,
        )

    def __call__(self, path: str, regex: str, file_pattern: Optional[str] = None, **kwargs) -> str:
        _result = functools.partial(
            self._result, path=path, regex=regex, file_pattern=file_pattern
        )

        # Input validation
        if not isinstance(path, str):
            return _result(ok=False, error={"type": "invalid_argument", "field": "path"})
        if not isinstance(regex, str):
            return _result(ok=False, error={"type": "invalid_argument", "field": "regex"})
        if file_pattern is not None and not isinstance(file_pattern, str):
            return _result(ok=False, error={"type": "invalid_argument", "field": "file_pattern"})

        if not os.path.exists(path):
            return _result(ok=False, error={"type": "not_found"})
        if not os.path.isdir(path):
            return _result(ok=False, error={"type": "not_a_directory"})

        file_matcher = self._create_file_matcher(file_pattern, base_path=path)

        results = []

        for root, _, files in os.walk(path):
            for filename in files:
                file_path = os.path.join(root, filename)
                if file_pattern and not file_matcher(file_path, path):
                    continue
                file_result = self._search_in_file(file_path, regex, context_num=5)
                if len(file_result) > 0:
                    results += file_result

        return _result(ok=True, matches=results, error=None)

    def _create_file_matcher(self, file_pattern: Optional[str], base_path: str):
        """Creates a matcher function based on glob-like file_pattern."""
        if not file_pattern:
            return lambda fp, bp: True

        glob_re = compile_glob(file_pattern)

        def matches(filepath: str, base_path: str) -> bool:
            filepath = filepath.replace(os.sep, '/')
            relative_path = os.path.relpath(filepath, base_path)
            return bool(glob_re.match(relative_path))

        return matches


def test_search_files_1():
    # Setup: create test directory and files
    test_dir = "./test_search_dir"
    os.makedirs(test_dir, exist_ok=True)

    # Create test files
    test_files = {
        "script.py": [
            "def hello():",
            "    print('Hello from Python')",
            "    value = 42",
            "    return value"
        ],
        "main.rs": [
            "fn main() {",
            "    println!(\"Hello from Rust\");",
            "    let answer = 42;",
            "}"
        ],
        "README.md": [
            "# Project with number 42",
            "This is a demo."
        ]
    }

    for fname, lines in test_files.items():
        with open(os.path.join(test_dir, fname), 'w', encoding='utf-8') as f:
            f.write("\n".join(lines))

    searcher = SearchFiles()

    # Test 1: Search for '42' in all files
    results = searcher(path=test_dir, regex="42")
    results = json.loads(results)
    assert len(results) >= 3  # At least 3 lines contain "42"
    assert any(r["file"].endswith("script.py") and "value = 42" in r["line_content"] for r in results)
    assert any(r["file"].endswith("main.rs") and "answer = 42" in r["line_content"] for r in results)
    assert any(r["file"].endswith("README.md") and "42" in r["line_content"] for r in results)

    # Test 2: Search only in *.py files
    results_py = searcher(path=test_dir, regex="Hello", file_pattern="*.py")
    results_py = json.loads(results_py)
    assert len(results_py) == 1
    assert results_py[0]["file"].endswith("script.py")

    # Test 3: Search in *.rs files
    results_rs = searcher(path=test_dir, regex="println", file_pattern="*.rs")
    results_rs = json.loads(results_rs)
    assert len(results_rs) == 1
    assert "println!" in results_rs[0]["line_content"]

    # Test 4: No matches
    results_none = searcher(path=test_dir, regex="^nonexistent$", file_pattern="*.txt")
    results_none = json.loads(results_none)
    assert isinstance(results_none, list)
    assert len(results_none) == 0

    # Cleanup
    shutil.rmtree(test_dir)

    print("All tests 1 passed.")

def test_search_files_2():
    # Setup: create test directory and files
    test_dir = "./test_search_dir"
    os.makedirs(test_dir, exist_ok=True)

    # Create test files
    test_files = {
        "script.py": [
            "def hello():",
            "    print('Hello from Python')",
            "    value = 42",
            "    return value"
        ],
        "main.rs": [
            "fn main() {",
            "    println!(\"Hello from Rust\");",
            "    let answer = 42;",
            "}"
        ],
        "README.md": [
            "# Project with number 42",
            "This is a demo."
        ],
        "empty.txt": [],
        "hidden.py": [
            "secret_token = 'abc123'",
            "debug_mode = True"
        ]
    }

    for fname, lines in test_files.items():
        with open(os.path.join(test_dir, fname), 'w', encoding='utf-8') as f:
            if lines:
                f.write("\n".join(lines) + "\n")

    searcher = SearchFiles()

    # Test 1: Search for '42' in all files
    results = searcher(path=test_dir, regex="42")
    results = json.loads(results)
    # print(results)
    assert len(results) >= 3
    assert any(r["file"].endswith("script.py") and "value = 42" == r["line_content"] for r in results)
    assert any(r["file"].endswith("main.rs") and "let answer = 42;" == r["line_content"] for r in results)
    assert any(r["file"].endswith("README.md") and "42" in r["line_content"] for r in results)

    # Test 2: Search only in *.py files
    results_py = json.loads(searcher(path=test_dir, regex="Hello", file_pattern="*.py"))
    assert len(results_py) == 1
    assert results_py[0]["file"].endswith("script.py")

    # Test 3: Search in *.rs files
    results_rs = json.loads(searcher(path=test_dir, regex="println", file_pattern="*.rs"))
    assert len(results_rs) == 1
    assert "println!" in results_rs[0]["line_content"]

    # Test 4: No matches
    results_none = json.loads(searcher(path=test_dir, regex="^nonexistent$", file_pattern="*.txt"))
    assert isinstance(results_none, list)
    assert len(results_none) == 0

    # Test 5: Case-sensitive match
    results_case = json.loads(searcher(path=test_dir, regex="heLLo"))
    assert len(results_case) == 0  # 'hello' lowercase not found

    # Test 6: Regex with escaping
    results_escaped = json.loads(searcher(path=test_dir, regex="print\\('"))
    assert len(results_escaped) == 1
    assert "print('Hello from Python')" == results_escaped[0]["line_content"]

    # Test 7: File pattern with subdirectory — create subdir
    subdir = os.path.join(test_dir, "src")
    os.makedirs(subdir, exist_ok=True)
    with open(os.path.join(subdir, "helper.py"), 'w') as f:
        f.write("def helper():\n    print('deep inside')\n")

    results_subdir = json.loads(searcher(path=test_dir, regex="deep", file_pattern="src/**/*.py"))
    # print(results_subdir)
    assert len(results_subdir) == 1
    assert "helper.py" in results_subdir[0]["file"]

    # Test 8: Invalid regex should raise error
    try:
        searcher(path=test_dir, regex="(")
        assert False, "Expected ValueError for invalid regex"
    except ValueError as e:
        assert "Invalid regex pattern" in str(e), f"Unexpected error message: {str(e)}"
    except Exception as e:
        assert False, f"Expected ValueError, but got {type(e)}: {e}"

    # Test 11: Context correctness
    results_ctx = json.loads(searcher(path=test_dir, regex="answer = 42;"))
    assert len(results_ctx) == 1
    ctx = results_ctx[0]["context"]
    assert "fn main() {" in ctx or "println!" in ctx or "}" in ctx

    # Test 12: Hidden file matched by pattern
    results_hidden = json.loads(searcher(path=test_dir, regex="secret", file_pattern="*.py"))
    assert len(results_hidden) == 1
    assert "hidden.py" in results_hidden[0]["file"]

    # Test 13: Use of \\d in regex (must use double backslash)
    results_digit = json.loads(searcher(path=test_dir, regex="\\d+"))
    # print(results_digit)
    assert len([r for r in results_digit if "42" in r["line_content"]]) >= 2

    # Test 14: Empty file handled correctly
    results_empty = json.loads(searcher(path=test_dir, regex="anything", file_pattern="empty.txt"))
    assert len(results_empty) == 0

    # Test 15: Literal hint optimization (edge case: no letters in regex)
    results_nohint = json.loads(searcher(path=test_dir, regex="\\s+=\\s+\\d+"))  # matches " = 42"
    # print(results_nohint)
    assert len(results_nohint) >= 2

    # Cleanup
    shutil.rmtree(test_dir)

    print("✅ All tests 2 passed.")

if __name__ == '__main__':
    test_search_files_1()
    test_search_files_2()
    pass
