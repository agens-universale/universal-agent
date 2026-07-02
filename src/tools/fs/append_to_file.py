import json
import os

from tools.schema_optional import X_PRESERVE_EMPTY_STRING
from tools.simple_tool_base import AbstractSimpleTool
from utils.tool_report_paths import tool_report_abspath
from security.path_policy import enforce_write_guard

class AppendToFile(AbstractSimpleTool):

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "fs_append_file",
            "description": (
                "Append text to the end of an existing file (optional create). "
                "Build large files with several moderate `content` chunks — not one huge append. "
                "Use after an initial write stub, or to add sections incrementally; read back to verify when needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The absolute path of the existing file to append to."
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Text to append; keep each call a moderate size (e.g. one section or a few KB). "
                            "For long output, chain many append-to-file calls instead of one huge payload."
                        ),
                    },
                    "separator": {
                        "type": "string",
                        X_PRESERVE_EMPTY_STRING: True,
                        "description": (
                            "String inserted before appended content. "
                            "Omit for default '\\n\\n'. "
                            "Pass empty string \"\" for no separator (append flush to file end)."
                        ),
                    },
                    "create_if_missing": {
                        "type": "boolean",
                        "description": "If true, creates the file if it does not exist. Default false."
                    },
                    "make_dirs": {
                        "type": "boolean",
                        "description": "If true, creates parent directories when missing (used with create_if_missing). Default false."
                    },
                    "encoding": {
                        "type": "string",
                        "description": "Text encoding to use. Default 'utf-8'."
                    }
                },
                "required": ["path", "content"],
                "additionalProperties": False
            },
            "strict": True
        }
        return description

    @classmethod
    def prompt_description(cls):
        description = '''Tool name: fs_append_file
Tool description: Appends content to an existing file (e.g., for notes, logs, analysis results).
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "Absolute path to an existing file."}, "content": {"type": "string", "description": "Content to append."}}, "required": ["path", "content"], "additionalProperties": false}

Usage:
- The target file MUST exist. If the file does not exist, write the file first (use a short initial write for new long files).
- **Chunking:** for large bodies, append in multiple steps; each `content` should be modest. Do not send an entire article or huge blob in one call.
- Optional `separator` before new content: default is a blank line (`\\n\\n`); pass empty string for no separator.
- Ideal for accumulating analysis notes, debugging logs, or review comments without overwriting previous data.
- Do not use for modifying source code logic.
'''
        return description

    @staticmethod
    def _result(
        *,
        ok: bool,
        path: str,
        separator: str,
        created: bool = False,
        bytes_appended: int = 0,
        error: dict | None = None,
    ) -> str:
        return json.dumps(
            {
                "tool": AppendToFile.registered_tool_name(),
                "ok": ok,
                "path": tool_report_abspath(path),
                "created": created,
                "bytes_appended": bytes_appended,
                "separator": separator,
                "error": error,
            },
            ensure_ascii=False,
        )

    def _write_guard_blocked_result(
        self,
        error: dict,
        path: str,
        content: str,
        separator: str = "\n\n",
        *args,
        **kwargs,
    ) -> str:
        return self._result(ok=False, path=path, separator=separator, error=error)

    @enforce_write_guard(path_arg="path", operation="append")
    def __call__(
        self,
        path: str,
        content: str,
        separator: str = "\n\n",
        create_if_missing: bool = False,
        make_dirs: bool = False,
        encoding: str = "utf-8",
        **kwargs,
    ) -> str:
        if not isinstance(path, str):
            return self._result(
                ok=False,
                path=str(path),
                separator=separator,
                error={"type": "invalid_argument", "field": "path"},
            )

        if not isinstance(content, str):
            return self._result(
                ok=False,
                path=path,
                separator=separator,
                error={"type": "invalid_argument", "field": "content"},
            )

        if not isinstance(separator, str):
            return self._result(
                ok=False,
                path=path,
                separator="",
                error={"type": "invalid_argument", "field": "separator"},
            )
        if not isinstance(create_if_missing, bool):
            return self._result(
                ok=False,
                path=path,
                separator=separator,
                error={"type": "invalid_argument", "field": "create_if_missing"},
            )
        if not isinstance(make_dirs, bool):
            return self._result(
                ok=False,
                path=path,
                separator=separator,
                error={"type": "invalid_argument", "field": "make_dirs"},
            )
        if not isinstance(encoding, str) or not encoding:
            return self._result(
                ok=False,
                path=path,
                separator=separator,
                error={"type": "invalid_argument", "field": "encoding"},
            )

        if not os.path.isabs(path):
            return self._result(
                ok=False,
                path=path,
                separator=separator,
                error={
                    "type": "invalid_argument",
                    "field": "path",
                    "message": "path must be absolute",
                },
            )

        # Проверка существования файла
        exists = self._backend.exists(path)
        if not exists:
            if not create_if_missing:
                return self._result(
                    ok=False,
                    path=path,
                    separator=separator,
                    error={"type": "not_found"},
                )
            parent = os.path.dirname(path) or "."
            if not self._backend.is_dir(parent):
                if make_dirs:
                    try:
                        self._backend.makedirs(parent)
                    except Exception as e:
                        return self._result(
                            ok=False,
                            path=path,
                            separator=separator,
                            error={"type": "mkdir_failed", "message": str(e)},
                        )
                else:
                    return self._result(
                        ok=False,
                        path=path,
                        separator=separator,
                        error={"type": "parent_not_found"},
                    )

        try:
            before = self._backend.getsize(path) if self._backend.exists(path) else 0
            self._backend.append(path, separator + content, encoding=encoding)
            after = self._backend.getsize(path)
            appended = max(0, after - before)
        except Exception as e:
            return self._result(
                ok=False,
                path=path,
                separator=separator,
                error={"type": "append_failed", "message": str(e)},
            )

        return self._result(
            ok=True,
            path=path,
            separator=separator,
            created=not exists,
            bytes_appended=appended,
            error=None,
        )
