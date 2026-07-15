import base64
import json
import os

from tools.simple_tool_base import AbstractSimpleTool
from utils.tool_report_paths import tool_report_abspath
from utils.atomic_io import atomic_replace


class SaveBase64File(AbstractSimpleTool):

    @classmethod
    def tool_description(cls):
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
    def prompt_description(cls):
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

    def _result(self, *, ok: bool, path: str, bytes_written: int = 0, error=None) -> str:
        return json.dumps(
            {
                "tool": SaveBase64File.registered_tool_name(),
                "ok": ok,
                "path": tool_report_abspath(path, workspace=self._workspace),
                "bytes_written": bytes_written,
                "error": error,
            },
            ensure_ascii=False,
        )

    def __call__(self, path: str, data: str, **kwargs) -> str:
        if not isinstance(path, str) or not path.strip():
            return self._result(ok=False, path=str(path), error={"type": "invalid_argument", "field": "path"})

        if not os.path.isabs(path):
            return self._result(ok=False, path=path, error={"type": "invalid_argument", "field": "path", "message": "path must be absolute"})

        if not isinstance(data, str) or not data.strip():
            return self._result(ok=False, path=path, error={"type": "invalid_argument", "field": "data"})

        try:
            raw = base64.b64decode(data, validate=False)
        except Exception as e:
            return self._result(ok=False, path=path, error={"type": "decode_failed", "message": str(e)})

        parent = os.path.dirname(path) or "."
        if not self._backend.is_dir(parent):
            return self._result(ok=False, path=path, error={"type": "parent_not_found"})

        try:
            self._backend.write_bytes(path, raw)
        except Exception as e:
            return self._result(ok=False, path=path, error={"type": "write_failed", "message": str(e)})

        return self._result(ok=True, path=path, bytes_written=len(raw))
