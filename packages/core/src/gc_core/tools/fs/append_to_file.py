import json
import os
from typing import Any, Dict, Optional

from gc_core.security.path_policy import enforce_write_guard
from gc_core.tools.schema_optional import X_PRESERVE_EMPTY_STRING
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.file_text import (
    APPEND_HEAD_SAMPLE_BYTES,
    check_encoding_on_file_head,
    resolve_encoding_arg,
    try_encode_text_bytes,
    try_encode_text_bytes_for_append,
)
from gc_core.utils.fs_size_limits import fs_mutate_max_bytes
from gc_core.utils.tool_report_paths import tool_report_abspath


def _validate_append_args(
    path, content, separator, create_if_missing, make_dirs, encoding
) -> "tuple[str | None, dict | None]":
    """(encoding_used, None) либо (None, error) на первом невалидном аргументе."""
    if not isinstance(content, str):
        return None, {"type": "invalid_argument", "field": "content"}
    if not isinstance(separator, str):
        return None, {"type": "invalid_argument", "field": "separator"}
    if not isinstance(create_if_missing, bool):
        return None, {"type": "invalid_argument", "field": "create_if_missing"}
    if not isinstance(make_dirs, bool):
        return None, {"type": "invalid_argument", "field": "make_dirs"}
    if not isinstance(encoding, str) or not encoding:
        return None, {"type": "invalid_argument", "field": "encoding"}
    encoding_used, enc_err = resolve_encoding_arg(encoding)
    if enc_err is not None:
        return None, enc_err
    if not os.path.isabs(path):
        return None, {
            "type": "invalid_argument",
            "field": "path",
            "message": "path must be absolute",
        }
    return encoding_used, None


def _written_bytes(
    written: str, chunk: str, encoding_used: str, payload_bytes: bytes
) -> bytes:
    """Байты, реально легшие на диск: при нормализации CRLF кусок другой.

    В журнал и в ``bytes_appended`` должно уйти именно это, а не исходный payload.
    """
    if written == chunk:
        return payload_bytes
    crlf_bytes, crlf_err = try_encode_text_bytes_for_append(
        written, encoding_used
    )
    if crlf_err is None and crlf_bytes is not None:
        return crlf_bytes
    return payload_bytes


def _encode_chunk(chunk: str, encoding_used: str, is_create: bool):
    # Create-path (incl. size==0 existing file): leading BOM needed for utf-16/sig.
    # Append-path: pure payload without BOM (mid-file BOM corrupts utf-16/sig).
    if is_create:
        return try_encode_text_bytes(chunk, encoding_used)
    return try_encode_text_bytes_for_append(chunk, encoding_used)


class AppendToFile(AbstractSimpleTool):
    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        description = {
            "type": "function",
            "name": "fs_append_file",
            "description": (
                "Append text to the end of an existing file (optional create). "
                "Checks encoding on a file head sample, then appends without loading "
                "the whole file into memory. "
                "Build large files with several moderate `content` chunks — not one huge append."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The absolute path of the existing file to append to.",
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
                            "Text put between the current file end and the new content. "
                            "Prefer two newlines unless another separator is needed; empty string means no separation. "
                            "Use a real newline character (embedded in the JSON string as \n), not a literal backslash-n. Do not double-escape."
                        ),
                    },
                    "create_if_missing": {
                        "type": "boolean",
                        "description": "If true, creates the file if it does not exist. Default false.",
                    },
                    "make_dirs": {
                        "type": "boolean",
                        "description": "If true, creates parent directories when missing (used with create_if_missing). Default false.",
                    },
                    "encoding": {
                        "type": "string",
                        "description": (
                            "Text encoding for the appended bytes: utf-8, utf-8-sig, cp1251, "
                            "koi8-r, latin-1, utf-16, utf-16-le, utf-16-be "
                            "(also windows-1251, iso-8859-1). Default utf-8. "
                            "Must match the existing file head; if the head cannot be decoded "
                            "as this encoding, append fails and the file is unchanged."
                        ),
                    },
                },
                "required": ["path", "content", "separator"],
                "additionalProperties": False,
            },
            "strict": True,
        }
        return description

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        description = """Tool name: fs_append_file
Tool description: Appends content to an existing file (e.g., for notes, logs, analysis results).
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "Absolute path to an existing file."}, "content": {"type": "string", "description": "Content to append."}, "separator": {"type": "string", "description": "Text put between the current file end and the new content. Prefer two newlines unless another separator is needed; empty string means no separation."}}, "required": ["path", "content", "separator"], "additionalProperties": false}

Usage:
- The target file MUST exist. If the file does not exist, write the file first (use a short initial write for new long files).
- **Chunking:** for large bodies, append in multiple steps; each `content` should be modest. Do not send an entire article or huge blob in one call.
- Ideal for accumulating analysis notes, debugging logs, or review comments without overwriting previous data.
- Do not use for modifying source code logic.
"""
        return description

    def _result(
        self,
        *,
        ok: bool,
        path: str,
        separator: str,
        created: bool = False,
        bytes_appended: int = 0,
        sha256: str | None = None,
        encoding_used: str | None = None,
        error: dict | None = None,
    ) -> str:
        return json.dumps(
            {
                "tool": AppendToFile.registered_tool_name(),
                "ok": ok,
                "path": tool_report_abspath(path, workspace=self._workspace),
                "created": created,
                "bytes_appended": bytes_appended,
                "sha256": sha256,
                "encoding_used": encoding_used,
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
        return self._result(
            ok=False, path=path, separator=separator, error=error
        )

    async def _missing_target_error(
        self, path: str, create_if_missing: bool, make_dirs: bool
    ) -> "dict | None":
        """Файла нет: можно ли создать (и родителя) — иначе готовая ошибка."""
        if not create_if_missing:
            return {"type": "not_found"}
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

    async def _existing_size(self, path: str) -> "tuple[int, dict | None]":
        try:
            return await self._backend.getsize(path), None
        except OSError as e:
            return 0, {"type": "stat_failed", "message": str(e)}

    async def _append_existing(
        self, path: str, chunk: str, encoding_used: str, size: int
    ) -> "tuple[dict | None, str]":
        """Дописать в существующий непустой файл.

        (ошибка проверки кодировки головы как dict | None, кусок, реально
        легший на диск — после нормализации CRLF).
        """
        head = await self._backend.read_head_bytes(
            path, APPEND_HEAD_SAMPLE_BYTES
        )
        head_err = check_encoding_on_file_head(
            head,
            encoding_used,
            sample_is_complete_file=size <= len(head),
        )
        if head_err is not None:
            return head_err, chunk
        # Preserve the existing newline style: if the file head uses
        # CRLF (\r\n), normalize lone \n in the appended chunk to \r\n
        # so we don't mix line endings. LF files are written as-is.
        if b"\r\n" in head:
            chunk = (
                chunk.replace("\r\n", "\n")
                .replace("\r", "\n")
                .replace("\n", "\r\n")
            )
        await self._backend.append(path, chunk, encoding=encoding_used)
        return None, chunk

    async def _journal_old_bytes(
        self, path: str, size: int, created_file: bool
    ) -> bytes | None:
        """Старый контент — ради baseline-снапшота и честного хэша в журнале.

        Тул сам файл целиком не читает, поэтому поднимаем его только когда
        запись правок ведётся и файл в пределах капа мутирующих тулов; иначе
        правка уйдёт в журнал как partial (кусок без baseline). У нового
        файла старого контента нет.
        """
        from gc_core.agent_edits.recorder import (  # nested: cycle with thread_store
            is_recording,
        )

        if created_file or not is_recording(self._ctx):
            return None
        try:
            if size <= fs_mutate_max_bytes(self._config_dict):
                return await self._backend.read_bytes(path)
        except Exception:
            return None
        return None

    async def _record(
        self,
        path: str,
        old_bytes: bytes | None,
        appended_bytes: bytes,
        created_file: bool,
    ) -> None:
        from gc_core.agent_edits.recorder import (  # nested: cycle with thread_store
            record_edit,
        )

        await record_edit(
            self._ctx,
            abs_path=path,
            old_bytes=old_bytes,
            new_bytes=appended_bytes
            if old_bytes is None
            else old_bytes + appended_bytes,
            op="create" if created_file else "append",
            call_id=self._call_id,
            # create: дописанное и есть весь файл; append без old: только кусок
            partial=old_bytes is None and not created_file,
        )

    @enforce_write_guard(path_arg="path", operation="append")
    async def acall(
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

        encoding_used, arg_error = _validate_append_args(
            path, content, separator, create_if_missing, make_dirs, encoding
        )
        if arg_error is not None:
            sep_out = separator if isinstance(separator, str) else ""
            return self._result(
                ok=False, path=path, separator=sep_out, error=arg_error
            )

        exists = await self._backend.exists(path)
        target_error = (
            None
            if exists
            else await self._missing_target_error(
                path, create_if_missing, make_dirs
            )
        )
        if target_error is not None:
            return self._result(
                ok=False, path=path, separator=separator, error=target_error
            )

        chunk = separator + content
        # Encode once according to whether we create or append.
        # Create-path (incl. size==0 existing file): leading BOM needed for utf-16/sig.
        # Append-path: pure payload without BOM (mid-file BOM corrupts utf-16/sig).
        size, size_error = (
            await self._existing_size(path) if exists else (0, None)
        )
        if size_error is not None:
            return self._result(
                ok=False, path=path, separator=separator, error=size_error
            )
        # ДВА разных вопроса, и раньше их нёс один флаг:
        #   `is_create`   — нужен ли ведущий BOM (у пустого файла нужен);
        #   `created_file` — новый ли это файл для ЖУРНАЛА.
        # Пустой существующий файл отвечает «да» на первый и «нет» на второй.
        # Слитые вместе, они делали `op="create"`, а откат такой записи файл
        # УДАЛЯЕТ, базу не спрашивая: пустой закоммиченный `__init__.py` после
        # дописывания и отката исчезал вместо возврата к пустому.
        created_file = not exists
        # Empty existing file behaves like create: needs a leading BOM.
        is_create = not exists or size == 0
        payload_bytes, encode_err = _encode_chunk(
            chunk, encoding_used, is_create
        )
        if encode_err is not None:
            return self._result(
                ok=False, path=path, separator=separator, error=encode_err
            )
        assert payload_bytes is not None

        old_bytes = await self._journal_old_bytes(path, size, created_file)
        appended_bytes = payload_bytes

        try:
            if is_create:
                await self._backend.write_bytes(path, payload_bytes)
            else:
                head_err, written = await self._append_existing(
                    path, chunk, encoding_used, size
                )
                if head_err is not None:
                    return self._result(
                        ok=False,
                        path=path,
                        separator=separator,
                        error=head_err,
                    )
                appended_bytes = _written_bytes(
                    written, chunk, encoding_used, payload_bytes
                )
            content_sha256 = await self._backend.sha256_bytes(path)
        except Exception as e:
            return self._result(
                ok=False,
                path=path,
                separator=separator,
                error={"type": "append_failed", "message": str(e)},
            )

        await self._record(path, old_bytes, appended_bytes, created_file)

        return self._result(
            ok=True,
            path=path,
            separator=separator,
            created=created_file,
            # appended_bytes, а не payload_bytes: при нормализации CRLF на диск
            # могло лечь больше байт, чем в исходном payload (см. запись в журнал выше).
            bytes_appended=len(appended_bytes),
            sha256=content_sha256,
            encoding_used=encoding_used,
            error=None,
        )
