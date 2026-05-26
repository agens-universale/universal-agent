import json
import os

from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool
from utils.tool_report_paths import tool_report_abspath
from security.path_policy import enforce_write_guard

class WriteToFile(AbstractSimpleTool):

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "write_to_file",
            "description": (
                "Writes text content to a file (optionally creating parent directories). By default overwrites the "
                "target file. Keep each call small: `content` must be short (outline, stub, or first section only). "
                "For long documents or articles, write a minimal file here, then append the rest with multiple "
                "`append_to_file` calls (moderate-sized chunks per call). One huge `content` often breaks tool-call "
                "JSON in chat APIs. Returns a JSON string with fields: ok, path, created, bytes_written, "
                "mode, backup_path, error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The absolute path of the file to write to. Must be an absolute path, not relative. Ensure the file location is valid and accessible."
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Text to write; must be short. For large output: use a placeholder or opening section "
                            "only, then add the bulk via several `append_to_file` calls. For code/config, keep to one "
                            "small file or split across write + appends."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "description": "Write mode: 'overwrite' (default), 'create_only' (fail if exists), or 'fail_if_missing' (fail if missing).",
                        "enum": ["overwrite", "create_only", "fail_if_missing"]
                    },
                    "make_dirs": {
                        "type": "boolean",
                        "description": "If true, creates parent directories when missing. Default false."
                    },
                    "encoding": {
                        "type": "string",
                        "description": "Text encoding to use. Default 'utf-8'."
                    },
                    "newline": {
                        "type": "string",
                        "description": "Newline normalization: 'preserve' (default), 'lf', or 'crlf'.",
                        "enum": ["preserve", "lf", "crlf"]
                    },
                    "backup": {
                        "type": "boolean",
                        "description": "If true and file exists, creates a backup copy at '<path>.bak' before writing. Default false."
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
        description = '''Tool name: write_to_file
Tool description: Writes a file to the local filesystem.
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "The absolute path of the file to write to (must be absolute, not relative)."}, "content": {"type": "string", "description": "The complete content to write to the file."}}, "required": ["path", "content"], "additionalProperties": false}

Usage:
- This tool will overwrite the existing file if there is one at the provided path.
- **Payload size:** put only brief `content` here (stub, headers, or first small section). For anything long, call `append_to_file` repeatedly with moderate chunks—never one massive `write_to_file`.
- If this is an existing file, you MUST use the Read tool first to read the file's contents. This tool will fail if you did not read the file first.
- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested by the User.
- Only use emojis if the user explicitly requests it. Avoid writing emojis to files unless asked.
'''
        return description

    @staticmethod
    def _result(
        *,
        ok: bool,
        path: str,
        mode: str,
        created: bool = False,
        bytes_written: int = 0,
        backup_path: str | None = None,
        error: dict | None = None,
    ) -> str:
        return json.dumps(
            {
                "tool": "write_to_file",
                "ok": ok,
                "path": tool_report_abspath(path),
                "mode": mode,
                "created": created,
                "bytes_written": bytes_written,
                "backup_path": tool_report_abspath(backup_path),
                "error": error,
            },
            ensure_ascii=False,
        )

    def _write_guard_blocked_result(
        self,
        error: dict,
        path: str,
        content: str,
        mode: str = "overwrite",
        *args,
        **kwargs,
    ) -> str:
        return self._result(ok=False, path=path, mode=mode, error=error)

    @enforce_write_guard(path_arg="path", operation="write")
    def __call__(
        self,
        path: str,
        content: str,
        mode: str = "overwrite",
        make_dirs: bool = False,
        encoding: str = "utf-8",
        newline: str = "preserve",
        backup: bool = False,
        **kwargs,
    ) -> str:
        if not isinstance(path, str):
            return self._result(
                ok=False,
                path=str(path),
                mode=mode,
                error={"type": "invalid_argument", "field": "path"},
            )

        if not isinstance(content, str):
            return self._result(
                ok=False,
                path=path,
                mode=mode,
                error={"type": "invalid_argument", "field": "content"},
            )

        if not isinstance(mode, str) or mode not in ("overwrite", "create_only", "fail_if_missing"):
            return self._result(
                ok=False,
                path=path,
                mode=mode,
                error={"type": "invalid_argument", "field": "mode"},
            )
        if not isinstance(make_dirs, bool):
            return self._result(
                ok=False,
                path=path,
                mode=mode,
                error={"type": "invalid_argument", "field": "make_dirs"},
            )
        if not isinstance(encoding, str) or not encoding:
            return self._result(
                ok=False,
                path=path,
                mode=mode,
                error={"type": "invalid_argument", "field": "encoding"},
            )
        if not isinstance(newline, str) or newline not in ("preserve", "lf", "crlf"):
            return self._result(
                ok=False,
                path=path,
                mode=mode,
                error={"type": "invalid_argument", "field": "newline"},
            )
        if not isinstance(backup, bool):
            return self._result(
                ok=False,
                path=path,
                mode=mode,
                error={"type": "invalid_argument", "field": "backup"},
            )

        if not os.path.isabs(path):
            return self._result(
                ok=False,
                path=path,
                mode=mode,
                error={
                    "type": "invalid_argument",
                    "field": "path",
                    "message": "path must be absolute",
                },
            )

        exists = os.path.exists(path)
        if mode == "create_only" and exists:
            return self._result(
                ok=False, path=path, mode=mode, error={"type": "already_exists"}
            )
        if mode == "fail_if_missing" and not exists:
            return self._result(
                ok=False, path=path, mode=mode, error={"type": "not_found"}
            )

        parent = os.path.dirname(path) or "."
        if not os.path.exists(parent):
            if make_dirs:
                try:
                    os.makedirs(parent, exist_ok=True)
                except Exception as e:
                    return self._result(
                        ok=False,
                        path=path,
                        mode=mode,
                        error={"type": "mkdir_failed", "message": str(e)},
                    )
            else:
                return self._result(
                    ok=False,
                    path=path,
                    mode=mode,
                    error={"type": "parent_not_found"},
                )

        normalized = content
        if newline == "lf":
            normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
        elif newline == "crlf":
            normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")

        backup_path = None
        if backup and exists:
            backup_path = f"{path}.bak"
            try:
                with open(path, "rb") as src, open(backup_path, "wb") as dst:
                    dst.write(src.read())
            except Exception as e:
                return self._result(
                    ok=False,
                    path=path,
                    mode=mode,
                    error={"type": "backup_failed", "message": str(e)},
                )

        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding=encoding, errors="replace") as fd:
                fd.write(normalized)
            os.replace(tmp_path, path)
        except Exception as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return self._result(
                ok=False,
                path=path,
                mode=mode,
                error={"type": "write_failed", "message": str(e)},
            )

        try:
            bytes_written = os.path.getsize(path)
        except Exception:
            bytes_written = 0

        return self._result(
            ok=True,
            path=path,
            mode=mode,
            created=not exists,
            bytes_written=bytes_written,
            backup_path=backup_path,
            error=None,
        )

