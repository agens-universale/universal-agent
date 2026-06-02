import base64
import json
import os

from tools.simple_tool_base import AbstractSimpleTool
from utils.tool_report_paths import tool_report_abspath


class SaveBase64File(AbstractSimpleTool):

    @classmethod
    def tool_description(cls):
        return {
            "type": "function",
            "name": "save_base64_file",
            "description": (
                "Decodes a base64-encoded string and saves it as a binary file. "
                "Use this to persist images (screenshots, etc.) received as base64 data from MCP tools. "
                "Returns JSON with fields: ok, path, bytes_written, error."
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
                        "description": "Base64-encoded content to decode and write.",
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
            "Tool name: save_base64_file\n"
            "Tool description: Decodes base64 data and saves it as a binary file (e.g. PNG screenshot).\n"
            'Input schema: {"type": "object", "properties": {"path": {"type": "string"}, '
            '"data": {"type": "string"}}, "required": ["path", "data"]}\n\n'
            "Usage:\n"
            "- Use this after browser_screenshot (or any tool returning base64 image data) "
            "to save the result to disk.\n"
            "- path must be absolute.\n"
            "- data is the raw base64 string (without the data URI prefix).\n"
        )

    @staticmethod
    def _result(*, ok: bool, path: str, bytes_written: int = 0, error=None) -> str:
        return json.dumps(
            {
                "tool": "save_base64_file",
                "ok": ok,
                "path": tool_report_abspath(path),
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
        if not os.path.exists(parent):
            return self._result(ok=False, path=path, error={"type": "parent_not_found"})

        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "wb") as fd:
                fd.write(raw)
            os.replace(tmp_path, path)
        except Exception as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return self._result(ok=False, path=path, error={"type": "write_failed", "message": str(e)})

        return self._result(ok=True, path=path, bytes_written=len(raw))
