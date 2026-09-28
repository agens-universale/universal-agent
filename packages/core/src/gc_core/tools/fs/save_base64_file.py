import base64
import json
import os
from typing import Any, Dict, Optional

from gc_core.security.path_policy import enforce_write_guard
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.file_text import sha256_hex_of_bytes
from gc_core.utils.fs_size_limits import fs_mutate_max_bytes
from gc_core.utils.tool_report_paths import tool_report_abspath


class SaveBase64File(AbstractSimpleTool):
    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        return {
            "type": "function",
            "name": "fs_save_base64_file",
            "description": (
                "Decodes a base64-encoded string and saves it as a binary file. "
                "Use this to persist images (screenshots, etc.) received as base64 data from MCP tools. "
                "data must be standard base64 (A-Z a-z 0-9 + / =), ASCII only, with correct padding; "
                "no data URI prefix. Returns JSON with fields: ok, path, bytes_written, error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path where the file will be saved.",
                    },
                    "data": {
                        "type": "string",
                        "description": (
                            "Raw standard base64 content (A-Z a-z 0-9 + / =), ASCII only, "
                            "with correct padding. Do not include a data URI prefix "
                            "(e.g. data:image/png;base64,)."
                        ),
                    },
                },
                "required": ["path", "data"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        return (
            "Tool name: fs_save_base64_file\n"
            "Tool description: Decodes base64 data and saves it as a binary file (e.g. PNG screenshot).\n"
            'Input schema: {"type": "object", "properties": {"path": {"type": "string"}, '
            '"data": {"type": "string"}}, "required": ["path", "data"]}\n\n'
            "Usage:\n"
            "- Use this after browser_screenshot (or any tool returning base64 image data) "
            "to save the result to disk.\n"
            "- path must be absolute.\n"
            "- data is the raw standard base64 string (A-Z a-z 0-9 + / =), ASCII only, "
            "with correct padding; no data URI prefix.\n"
            "- On decode_failed, check for non-ASCII characters or incorrect padding.\n"
        )

    def _result(
        self,
        *,
        ok: bool,
        path: str,
        bytes_written: int = 0,
        sha256=None,
        error=None,
    ) -> str:
        return json.dumps(
            {
                "tool": SaveBase64File.registered_tool_name(),
                "ok": ok,
                "path": tool_report_abspath(path, workspace=self._workspace),
                "bytes_written": bytes_written,
                "sha256": sha256,
                "error": error,
            },
            ensure_ascii=False,
        )

    def _write_guard_blocked_result(
        self,
        error: dict,
        path: str,
        data: str,
        *args,
        **kwargs,
    ) -> str:
        return self._result(ok=False, path=path, error=error)

    @enforce_write_guard(path_arg="path", operation="write")
    async def acall(self, path: str, data: str, **kwargs) -> str:
        if not isinstance(path, str) or not path.strip():
            return self._result(
                ok=False,
                path=str(path),
                error={"type": "invalid_argument", "field": "path"},
            )

        if not os.path.isabs(path):
            return self._result(
                ok=False,
                path=path,
                error={
                    "type": "invalid_argument",
                    "field": "path",
                    "message": "path must be absolute",
                },
            )

        if not isinstance(data, str) or not data.strip():
            return self._result(
                ok=False,
                path=path,
                error={"type": "invalid_argument", "field": "data"},
            )

        try:
            raw = base64.b64decode(data, validate=False)
        except Exception as e:
            return self._result(
                ok=False,
                path=path,
                error={"type": "decode_failed", "message": str(e)},
            )

        parent = os.path.dirname(path) or "."
        if not await self._backend.is_dir(parent):
            return self._result(
                ok=False, path=path, error={"type": "parent_not_found"}
            )

        from gc_core.agent_edits.recorder import (  # nested: cycle with thread_store
            is_recording,
            record_edit,
        )

        # Контент до перезаписи — для журнала правок: без него baseline-снапшот
        # не снимается и старое содержимое теряется навсегда. Читаем только
        # когда запись правок реально ведётся и файл в пределах капа мутирующих
        # тулов (тот же паттерн, что в WriteToFile).
        exists = await self._backend.exists(path)
        old_bytes = None
        if exists and is_recording(self._ctx):
            try:
                if await self._backend.getsize(path) <= fs_mutate_max_bytes(
                    self._config_dict
                ):
                    old_bytes = await self._backend.read_bytes(path)
            except Exception:
                old_bytes = None

        try:
            await self._backend.write_bytes(path, raw)
        except Exception as e:
            return self._result(
                ok=False,
                path=path,
                error={"type": "write_failed", "message": str(e)},
            )

        # op — по факту существования файла: бинарность контента классифицирует
        # сам рекордер (иначе текстовый payload получил бы op="binary" с хэнками,
        # но без флага no_diff — контракт «binary ⇒ hunks пуст + no_diff» сломан).
        await record_edit(
            self._ctx,
            abs_path=path,
            old_bytes=old_bytes,
            new_bytes=raw,
            op="write" if exists else "create",
            call_id=self._call_id,
        )

        return self._result(
            ok=True,
            path=path,
            bytes_written=len(raw),
            sha256=sha256_hex_of_bytes(raw),
        )
