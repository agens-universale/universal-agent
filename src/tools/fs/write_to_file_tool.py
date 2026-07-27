import hashlib
import json
import os

from tools.simple_tool_base import AbstractSimpleTool
from utils.tool_report_paths import tool_report_abspath
from utils.atomic_io import atomic_replace
from security.path_policy import enforce_write_guard
class WriteToFile(AbstractSimpleTool):

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "fs_write_file",
            "description": (
                "Create or overwrite a text file (optional parent dirs). Keep each call small — stub or first section only. "
                "For long documents: minimal write here, then chain append calls with moderate chunks. "
                "Huge single `content` often breaks tool-call JSON. "
                "Prefer this when you need a new file or full replace; use append to grow an existing file."
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
                            "only, then add the bulk via several append-to-file calls. For code/config, keep to one "
                            "small file or split across an initial write and appends."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "description": "Write mode: 'overwrite' (default), 'create_only' (fail if exists), or 'fail_if_missing' (fail if missing).",
                        "enum": ["overwrite", "create_only", "fail_if_missing"]
                    },
                    "make_dirs": {
                        "type": "boolean",
                        "description": "If true, creates any missing parent folders before writing. Set true when the target file's parent folder does not exist yet. Default false — the write fails with 'parent_not_found' if the parent folder is missing."
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
        description = '''Tool name: fs_write_file
Tool description: Writes a file to the local filesystem.
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "The absolute path of the file to write to (must be absolute, not relative)."}, "content": {"type": "string", "description": "The complete content to write to the file."}, "make_dirs": {"type": "boolean", "description": "If true, create any missing parent folders before writing. Default false."}}, "required": ["path", "content"], "additionalProperties": false}

Usage:
- This tool will overwrite the existing file if there is one at the provided path.
- To write a file into a folder that does not exist yet, pass make_dirs: true — it creates the missing parent folders. Without it, the write fails when the parent folder is missing.
- **Payload size:** put only brief `content` here (stub, headers, or first small section). For anything long, append to the file repeatedly in moderate chunks—never one massive single write.
- If this is an existing file, you MUST read the file first (filesystem read). This tool will fail if you did not read the file first.
- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested by the User.
- Only use emojis if the user explicitly requests it. Avoid writing emojis to files unless asked.
'''
        return description

    def _result(self, 
        *,
        ok: bool,
        path: str,
        mode: str,
        created: bool = False,
        bytes_written: int = 0,
        backup_path: str | None = None,
        sha256: str | None = None,
        error: dict | None = None,
    ) -> str:
        return json.dumps(
            {
                "tool": WriteToFile.registered_tool_name(),
                "ok": ok,
                "path": tool_report_abspath(path, workspace=self._workspace),
                "mode": mode,
                "created": created,
                "bytes_written": bytes_written,
                "sha256": sha256,
                "backup_path": tool_report_abspath(backup_path, workspace=self._workspace),
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

        exists = self._backend.exists(path)
        if mode == "create_only" and exists:
            return self._result(
                ok=False, path=path, mode=mode, error={"type": "already_exists"}
            )
        if mode == "fail_if_missing" and not exists:
            return self._result(
                ok=False, path=path, mode=mode, error={"type": "not_found"}
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
                self._backend.copy(path, backup_path)
            except Exception as e:
                return self._result(
                    ok=False,
                    path=path,
                    mode=mode,
                    error={"type": "backup_failed", "message": str(e)},
                )

        try:
            self._backend.write(path, normalized, encoding=encoding)
        except Exception as e:
            return self._result(
                ok=False,
                path=path,
                mode=mode,
                error={"type": "write_failed", "message": str(e)},
            )

        try:
            bytes_written = self._backend.getsize(path)
        except Exception:
            bytes_written = 0

        # Hash the exact bytes written (same encoding/errors as backend.write).
        content_sha256 = hashlib.sha256(
            normalized.encode(encoding, errors="replace")
        ).hexdigest()

        return self._result(
            ok=True,
            path=path,
            mode=mode,
            created=not exists,
            bytes_written=bytes_written,
            sha256=content_sha256,
            backup_path=backup_path,
            error=None,
        )

