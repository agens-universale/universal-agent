import os
import json
import functools
from typing import Dict, Any
from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool
from utils.tool_report_paths import tool_report_abspath


class SearchAndReplace(AbstractSimpleTool):
    """
    Tool: search_and_replace
    Performs a single search and replace operation in a file.
    Only the first occurrence is replaced.
    """

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "search_and_replace",
            "description": (
                "Replaces the first occurrence of exact text in a file. Only one replacement per call. "
                "Uses literal string matching (case-sensitive, no regex). "
                "Always returns a JSON string with fields: ok, matched, replaced, path, message, error. "
                "ok=true only when the file was modified."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The absolute path of the file to modify. Must be a valid file path; directories are not allowed."
                    },
                    "search": {
                        "type": "string",
                        "description": "The exact content to find and replace. Match is case-sensitive and literal (not regex). May be multiline. Cannot be empty."
                    },
                    "replace": {
                        "type": "string",
                        "description": "The new content to replace the found content with. May be multiline. Can be empty to remove the matched content."
                    }
                },
                "required": ["path", "search", "replace"],
                "additionalProperties": False
            },
            "strict": True
        }

    @classmethod
    def prompt_description(cls) -> str:
        description = '''Tool name: search_and_replace  
Tool description: Replaces the **first occurrence** of exact text in a file. Only one replacement per call.
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "The absolute path of the file to modify."}, "search": {"type": "string", "description": "The exact content to find and replace.  May be multiline."}, "replace": {"type": "string", "description": "The new content to replace the found content with.  May be multiline."}}, "required": ["path", "search", "replace"], "additionalProperties": false}

Usage:
- Modifies a **single file** by replacing the **first occurrence** of `search` string with `replace`.
- The `search` string must match **exactly** (case-sensitive).
- Only the **first match** is replaced — subsequent matches are left unchanged.
- File is rewritten safely (write + replace) to prevent data loss.
- Uses UTF-8 encoding with `errors='ignore'` for robustness.

Important Usage Rules
- **ALWAYS** use absolute paths for `path`.
- The `search` string is treated as **literal text**, not regex.
- To perform regex replacements, use `search_files` first to locate, then decide on literal replacement.
- If the file does not contain the `search` string, no changes are made.
- If the file is empty or unreadable, an error is raised.
- This tool ALWAYS returns a JSON string with fields:
  - ok: boolean — overall success (true means file was modified).
  - matched: boolean — whether `search` was found in the file.
  - replaced: integer — number of replacements applied (0 or 1).
  - path: string — the file path.
  - message: string — human-readable summary.
  - error: object|null — error details when ok=false due to an error (not "not found").
- **NEVER** use shell commands like `sed`, `perl -pi`, etc. — this tool is safe and cross-platform.
- For multiline replacements use line feed characters (e.g. \\n, \\r\\n depending on the platform)  in literal text for arguments `search` and `replace`

Examples
Replace database URL in config:
{
  "path": "/home/user/app/config.json",
  "search": "localhost:5432",
  "replace": "db.prod.internal:5432"
}

Fix a typo in source code:
{
  "path": "/home/user/project/main.py",
  "search": "prin('Hello')",
  "replace": "print('Hello')"
}

Update version string:
{
  "path": "/home/user/project/README.md",
  "search": "Version\\n1.0.0",
  "replace": "Version\\n1.0.1"
}
'''
        return description

    @staticmethod
    def _result(
        *,
        path: str,
        ok: bool,
        matched: bool,
        replaced: int,
        message: str,
        error: Dict[str, Any] | None = None,
    ) -> str:
        return json.dumps(
            {
                "tool": "search_and_replace",
                "ok": ok,
                "matched": matched,
                "replaced": replaced,
                "path": tool_report_abspath(path),
                "error": error,
                "message": message,
            },
            ensure_ascii=False,
        )

    def __call__(self, path: str, search: str, replace: str, **kwargs) -> str:
        _result = functools.partial(self._result, path=path)

        # Input validation
        if not isinstance(path, str):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Argument 'path' must be a string.",
                error={"type": "invalid_argument", "field": "path"},
            )
        if not isinstance(search, str):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Argument 'search' must be a string.",
                error={"type": "invalid_argument", "field": "search"},
            )
        if not isinstance(replace, str):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Argument 'replace' must be a string.",
                error={"type": "invalid_argument", "field": "replace"},
            )

        if not path:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Argument 'path' cannot be an empty string.",
                error={"type": "invalid_argument", "field": "path"},
            )
        if not os.path.exists(path):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="File not found.",
                error={"type": "not_found"},
            )
        if not os.path.isfile(path):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Path is not a file.",
                error={"type": "not_a_file"},
            )

        if search == '':
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Argument 'search' cannot be an empty string.",
                error={"type": "invalid_argument", "field": "search"},
            )

        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except Exception as e:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Failed to read file.",
                error={"type": "read_failed", "message": str(e)},
            )

        # Check if search string exists
        if search not in content:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Search string not found in file. No changes made.",
                error=None,
            )

        # Perform replacement (only first occurrence)
        new_content = content.replace(search, replace, 1)

        # Write back only if changed
        if new_content == content:
            return _result(
                ok=False,
                matched=True,
                replaced=0,
                message="No change needed (content identical after replacement).",
                error=None,
            )

        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, 'w', encoding='utf-8', errors='replace') as f:
                f.write(new_content)
            os.replace(tmp_path, path)
        except Exception as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return _result(
                ok=False,
                matched=True,
                replaced=0,
                message="Failed to write file.",
                error={"type": "write_failed", "message": str(e)},
            )

        return _result(
            ok=True,
            matched=True,
            replaced=1,
            message="Text replaced successfully.",
            error=None,
        )
