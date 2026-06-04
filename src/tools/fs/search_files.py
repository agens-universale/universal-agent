import json
import functools
import os
import re
from typing import Optional, List, Dict, Any, Tuple
from tools.schema_optional import coalesce_optional_string
from tools.simple_tool_base import AbstractSimpleTool
from utils.glob_to_regex import compile_glob
from utils.tool_report_paths import tool_report_abspath


class SearchFiles(AbstractSimpleTool):
    """Paginated grep-style search; default first page mirrors fs_read_file preview (500 items)."""

    default_limit = 500
    _literal_word_re = re.compile(r"[a-zA-Z_а-яА-Я][a-zA-Z0-9_а-яА-Я]+")

    @classmethod
    def tool_description(cls):
        return {
            "type": "function",
            "name": "fs_search_files",
            "description": (
                "Recursively searches for a regex pattern in file contents under a directory, optionally filtered "
                "by a glob pattern. Returns paginated hits: metadata includes total_matches, offset, limit, truncated, "
                "next_offset, then matches (slice for this page). Field count is the number of matches in this "
                "response (same as len(matches)); total_matches is the full hit count across the directory."
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
                        "description": (
                            "Glob pattern to filter files (e.g., '*.py', 'src/**/*.rs'). "
                            "When unset, all files are searched."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "Optional 1-based index of the first match to return (among all matches ordered by walk). "
                            f"Defaults to 1 together with limit. When both offset and limit are omitted, offsets 1 "
                            f"and uses default limit ({cls.default_limit})."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Optional maximum number of matches to return starting from offset. "
                            f"If omitted but offset is provided, defaults to {cls.default_limit}. "
                            "If both offset and limit are omitted, defaults to offset=1 and the same limit."
                        ),
                    },
                },
                "required": ["path", "regex"],
                "additionalProperties": False
            },
            "strict": True
        }

    @classmethod
    def prompt_description(cls):
        description = '''Tool name: fs_search_files  
Tool description: Recursively searches for a regex pattern in all files within a specified directory, optionally filtered by a glob pattern.
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "The directory to search in."}, "regex": {"type": "string", "description": "The Rust-style regex pattern to search for."}, "file_pattern": {"type": "string", "description": "Glob pattern to filter files (e.g., \'*.py\')."}, "offset": {"type": "integer", "description": "1-based index of the first match to return."}, "limit": {"type": "integer", "description": "Max matches returned for this page."}}, "required": ["path", "regex"], "additionalProperties": false}

Usage:
- Searches **all files** recursively under the given directory.
- Filters files using a **glob pattern** (e.g., `"*.py"`, `"src/**/*.rs"`).
- Matches lines using a **Rust-style regular expression**.
- Returns a **paginated** list of matches with file paths and line contents.
- **`count`** = matches in **this response** (page size). **`total_matches`** = all hits in the scan.
- If **`truncated`** is true, call again with **`offset` = `next_offset`** (and same or new `limit`) to get more.
- Omitting **`offset`** and **`limit`** is equivalent to the first page: offset 1 and default **`limit`** (same default window idea as **fs_read_file** preview).
- Returns an empty **`matches`** list if none found.

Important Usage Rules
- **ALWAYS** use `fs_search_files` for searching text or code.
- **NEVER** use external tools like `grep`, `rg`, or shell commands — this tool is optimized and permission-safe.
- Always provide a **full absolute path** for the `path` parameter.
- The `regex` must be a valid Rust-compatible pattern. Avoid unsafe features:
  - No backreferences (e.g., `\1`, `\2`)
  - No variable-length lookarounds (e.g., `(?<=a.*)b`)
  - No recursive or deeply nested patterns
- **No multiline matching**: patterns must match within a **single line**.
- Use **simple, efficient regex patterns** to avoid performance issues or excessive results.
- Always escape special sequences with **double backslashes**: use `\\d`, `\\s`, `\\w`, `\\.` instead of `\\d`, `\\s`, etc.
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

    def _extract_literals(self, pattern: str) -> Tuple[str, ...]:
        """Буквенные фрагменты паттерна для быстрой фильтрации строк (OR, не AND)."""
        seen: set[str] = set()
        literals: List[str] = []
        for match in self._literal_word_re.finditer(pattern):
            word = match.group(0)
            if word not in seen:
                seen.add(word)
                literals.append(word)
        return tuple(literals)

    def _search_in_file(self, file_path: str, regex: str, context_num=5) -> List[Dict[str, Any]]:
        try:
            compiled_regex = re.compile(regex)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern '{regex}': {str(e)}")

        literal_hints = self._extract_literals(regex)

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

                    if literal_hints and not any(hint in line for hint in literal_hints):
                        buffer = buffer[-context_num:]
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
                            "file": tool_report_abspath(file_path),
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
        total_matches: Optional[int] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        truncated: Optional[bool] = None,
        next_offset: Optional[int] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "tool": SearchFiles.registered_tool_name(),
            "ok": ok,
            "path": tool_report_abspath(path),
            "regex": regex,
            "file_pattern": file_pattern,
            "error": error,
            "matches": matches if matches is not None else [],
        }
        if ok and total_matches is not None:
            payload["total_matches"] = total_matches
            payload["count"] = len(matches) if matches is not None else 0
            payload["offset"] = offset
            payload["limit"] = limit
            payload["truncated"] = bool(truncated)
            payload["next_offset"] = next_offset
        elif not ok:
            payload["total_matches"] = None
            payload["count"] = 0
            payload["offset"] = None
            payload["limit"] = None
            payload["truncated"] = None
            payload["next_offset"] = None

        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def _resolve_slice(
        cls, total: int, offset: Optional[int], limit: Optional[int]
    ) -> Tuple[bool, Optional[Dict[str, Any]], int, int, int]:
        """Return ok, error, start_0, limit_eff, offset_effective."""
        if offset is None and limit is None:
            off_eff = 1
            lim_eff = cls.default_limit
        elif offset is None:
            off_eff = 1
            lim_eff = limit if limit is not None else cls.default_limit
        elif limit is None:
            off_eff = offset
            lim_eff = cls.default_limit
        else:
            off_eff = offset
            lim_eff = limit

        if off_eff < 1:
            return False, {"type": "invalid_argument", "field": "offset", "message": "offset must be >= 1"}, 0, 0, 0
        if lim_eff < 1:
            return False, {"type": "invalid_argument", "field": "limit", "message": "limit must be >= 1"}, 0, 0, 0

        start_0 = off_eff - 1
        if start_0 >= total:
            start_0 = total

        return True, None, start_0, lim_eff, off_eff

    def __call__(
        self,
        path: str,
        regex: str,
        file_pattern: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        **kwargs,
    ) -> str:
        file_pattern = coalesce_optional_string(file_pattern)
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
        if offset is not None and not isinstance(offset, int):
            return _result(ok=False, error={"type": "invalid_argument", "field": "offset"})
        if limit is not None and not isinstance(limit, int):
            return _result(ok=False, error={"type": "invalid_argument", "field": "limit"})

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

        total_matches = len(results)
        slice_ok, slice_err, start_0, lim_eff, off_eff = self._resolve_slice(
            total_matches, offset, limit
        )
        if not slice_ok:
            return _result(ok=False, error=slice_err)

        end_0 = min(start_0 + lim_eff, total_matches)
        page = results[start_0:end_0]
        truncated = end_0 < total_matches
        next_offset = end_0 + 1 if truncated else None

        return _result(
            ok=True,
            matches=page,
            error=None,
            total_matches=total_matches,
            offset=off_eff,
            limit=lim_eff,
            truncated=truncated,
            next_offset=next_offset,
        )

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
