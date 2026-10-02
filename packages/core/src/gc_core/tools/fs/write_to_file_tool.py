import json
import os
from typing import Any, Dict, Optional

from gc_core.security.path_policy import enforce_write_guard
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.file_text import (
    resolve_encoding_arg,
    sha256_hex_of_bytes,
    try_encode_text_bytes,
)
from gc_core.utils.fs_size_limits import fs_mutate_max_bytes
from gc_core.utils.tool_report_paths import tool_report_abspath


def _validate_write_args(
    path, content, mode, make_dirs, encoding, newline, backup
) -> "tuple[str | None, dict | None]":
    """(encoding_used, None) либо (None, error) на первом невалидном аргументе."""
    if not isinstance(content, str):
        return None, {"type": "invalid_argument", "field": "content"}
    if not isinstance(mode, str) or mode not in (
        "overwrite",
        "create_only",
        "fail_if_missing",
    ):
        return None, {"type": "invalid_argument", "field": "mode"}
    if not isinstance(make_dirs, bool):
        return None, {"type": "invalid_argument", "field": "make_dirs"}
    if not isinstance(encoding, str) or not encoding:
        return None, {"type": "invalid_argument", "field": "encoding"}
    encoding_used, enc_err = resolve_encoding_arg(encoding)
    if enc_err is not None:
        return None, enc_err
    if not isinstance(newline, str) or newline not in (
        "preserve",
        "lf",
        "crlf",
    ):
        return None, {"type": "invalid_argument", "field": "newline"}
    if not isinstance(backup, bool):
        return None, {"type": "invalid_argument", "field": "backup"}
    if not os.path.isabs(path):
        return None, {
            "type": "invalid_argument",
            "field": "path",
            "message": "path must be absolute",
        }
    return encoding_used, None


def _normalize_newlines(content: str, newline: str) -> str:
    if newline == "lf":
        return content.replace("\r\n", "\n").replace("\r", "\n")
    if newline == "crlf":
        return (
            content.replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", "\r\n")
        )
    return content


class WriteToFile(AbstractSimpleTool):
    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        description = {
            "type": "function",
            "name": "fs_write_file",
            "description": (
                "Create or overwrite a text file (optional parent dirs). Keep each call small — stub or first section only. "
                "For long documents: minimal write here, then chain append calls with moderate chunks. "
                "Huge single `content` often breaks tool-call JSON. "
                "Prefer this when you need a new file or full replace; use append to grow an existing file. "
                "Pass encoding= to write non-UTF-8 (e.g. cp1251)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The absolute path of the file to write to. Must be an absolute path, not relative. Ensure the file location is valid and accessible.",
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
                        "enum": [
                            "overwrite",
                            "create_only",
                            "fail_if_missing",
                        ],
                    },
                    "make_dirs": {
                        "type": "boolean",
                        "description": "If true, creates any missing parent folders before writing. Set true when the target file's parent folder does not exist yet. Default false — the write fails with 'parent_not_found' if the parent folder is missing.",
                    },
                    "encoding": {
                        "type": "string",
                        "description": (
                            "Text encoding to write: utf-8, utf-8-sig, cp1251, koi8-r, "
                            "latin-1, utf-16, utf-16-le, utf-16-be "
                            "(also windows-1251, iso-8859-1). Default utf-8."
                        ),
                    },
                    "newline": {
                        "type": "string",
                        "description": "Newline normalization: 'preserve' (default), 'lf', or 'crlf'.",
                        "enum": ["preserve", "lf", "crlf"],
                    },
                    "backup": {
                        "type": "boolean",
                        "description": "If true and file exists, creates a backup copy at '<path>.bak' before writing. Default false.",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            "strict": True,
        }
        return description

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        description = """Tool name: fs_write_file
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
"""
        return description

    def _result(
        self,
        *,
        ok: bool,
        path: str,
        mode: str,
        created: bool = False,
        bytes_written: int = 0,
        backup_path: str | None = None,
        sha256: str | None = None,
        encoding_used: str | None = None,
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
                "encoding_used": encoding_used,
                "backup_path": tool_report_abspath(
                    backup_path, workspace=self._workspace
                ),
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

    async def _ensure_parent_dir(
        self, path: str, make_dirs: bool
    ) -> "dict | None":
        parent = os.path.dirname(path) or "."
        if await self._backend.is_dir(parent):
            return None
        if not make_dirs:
            return {"type": "parent_not_found"}
        try:
            await self._backend.makedirs(parent)
        except Exception as e:
            return {"type": "mkdir_failed", "message": str(e)}
        return None

    async def _journal_old_bytes(
        self, path: str, exists: bool
    ) -> bytes | None:
        """Контент до перезаписи — для журнала правок.

        Тул нигде больше его не читает, а без него запись о перезаписи
        выглядит как создание файла (нет baseline-снапшота, весь файл в дифф
        как добавленный). Читаем только когда запись правок реально ведётся и
        файл в пределах капа мутирующих тулов — собственного гейта по размеру
        у этого тула нет.
        """
        from gc_core.agent_edits.recorder import (  # nested: cycle with thread_store
            is_recording,
        )

        if not exists or not is_recording(self._ctx):
            return None
        try:
            if await self._backend.getsize(path) <= fs_mutate_max_bytes(
                self._config_dict
            ):
                return await self._backend.read_bytes(path)
        except Exception:
            return None
        return None

    @enforce_write_guard(path_arg="path", operation="write")
    async def acall(
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
        encoding_used, arg_error = _validate_write_args(
            path, content, mode, make_dirs, encoding, newline, backup
        )
        if arg_error is not None:
            return self._result(
                ok=False, path=path, mode=mode, error=arg_error
            )

        exists = await self._backend.exists(path)
        if mode == "create_only" and exists:
            return self._result(
                ok=False,
                path=path,
                mode=mode,
                error={"type": "already_exists"},
            )
        if mode == "fail_if_missing" and not exists:
            return self._result(
                ok=False, path=path, mode=mode, error={"type": "not_found"}
            )

        parent_error = await self._ensure_parent_dir(path, make_dirs)
        if parent_error is not None:
            return self._result(
                ok=False, path=path, mode=mode, error=parent_error
            )

        normalized = _normalize_newlines(content, newline)

        backup_path = None
        if backup and exists:
            backup_path = f"{path}.bak"
            try:
                await self._backend.copy(path, backup_path)
            except Exception as e:
                return self._result(
                    ok=False,
                    path=path,
                    mode=mode,
                    error={"type": "backup_failed", "message": str(e)},
                )

        from gc_core.agent_edits.recorder import (  # nested: cycle with thread_store
            record_edit,
        )

        old_bytes = await self._journal_old_bytes(path, exists)

        try:
            out_bytes, encode_err = try_encode_text_bytes(
                normalized, encoding_used
            )
            if encode_err is not None:
                return self._result(
                    ok=False,
                    path=path,
                    mode=mode,
                    backup_path=backup_path,
                    error=encode_err,
                )
            assert out_bytes is not None
            await self._backend.write_bytes(path, out_bytes)
        except Exception as e:
            return self._result(
                ok=False,
                path=path,
                mode=mode,
                backup_path=backup_path,
                error={"type": "write_failed", "message": str(e)},
            )

        await record_edit(
            self._ctx,
            abs_path=path,
            old_bytes=old_bytes,
            new_bytes=out_bytes,
            op="write" if exists else "create",
            call_id=self._call_id,
        )

        return self._result(
            ok=True,
            path=path,
            mode=mode,
            created=not exists,
            bytes_written=len(out_bytes),
            sha256=sha256_hex_of_bytes(out_bytes),
            encoding_used=encoding_used,
            backup_path=backup_path,
            error=None,
        )
