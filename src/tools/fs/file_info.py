import json
import os
import stat as stat_module
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from utils.file_text import (
    count_lines_in_text,
    guess_is_binary,
    read_text_for_edit,
    sha256_hex_of_file,
)
from tools.simple_tool_base import AbstractSimpleTool
from utils.tool_report_paths import tool_report_abspath

DEFAULT_MAX_HASH_BYTES = 100 * 1024 * 1024


class FileInfo(AbstractSimpleTool):
    max_hash_bytes = DEFAULT_MAX_HASH_BYTES

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "file_info",
            "description": (
                "Read-only file metadata for preflight before edits: type, size, mtime, "
                "permissions, readability, binary/text guess, optional line count, SHA-256."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "include_hash": {"type": "boolean"},
                    "hash_algorithm": {"type": "string", "enum": ["sha256"]},
                    "include_line_count": {"type": "boolean"},
                    "follow_symlinks": {"type": "boolean"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls) -> str:
        return (
            "Tool name: file_info\n"
            "Cheap read-only metadata before editing a file.\n"
            "Use include_hash=true to get sha256 for expected_file_sha256 in search_and_replace / apply_file_edits.\n"
            "Does not return full file content — use read_file for that.\n"
        )

    @staticmethod
    def _result(**fields: Any) -> str:
        payload = {"tool": "file_info", "error": None, **fields}
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _error(path: str, error: Dict[str, Any], message: str) -> str:
        return json.dumps(
            {
                "tool": "file_info",
                "ok": False,
                "path": tool_report_abspath(path),
                "exists": False,
                "message": message,
                "error": error,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _format_permissions(mode: int) -> str:
        def triplet(bits: int) -> str:
            return (
                ("r" if mode & bits else "-")
                + ("w" if mode & (bits >> 1) else "-")
                + ("x" if mode & (bits >> 2) else "-")
            )

        return triplet(stat_module.S_IRUSR) + triplet(stat_module.S_IRGRP) + triplet(stat_module.S_IROTH)

    @staticmethod
    def _mtime_iso(st: os.stat_result) -> str:
        return datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def __call__(
        self,
        path: str,
        include_hash: bool = False,
        hash_algorithm: str = "sha256",
        include_line_count: bool = False,
        follow_symlinks: bool = False,
        **kwargs,
    ) -> str:
        if not isinstance(path, str) or not path:
            return self._error(
                path=str(path),
                error={"type": "invalid_argument", "field": "path"},
                message="Argument 'path' must be a non-empty string.",
            )
        if not isinstance(include_hash, bool):
            return self._error(
                path=path,
                error={"type": "invalid_argument", "field": "include_hash"},
                message="Argument 'include_hash' must be a boolean.",
            )
        if hash_algorithm != "sha256":
            return self._error(
                path=path,
                error={"type": "invalid_argument", "field": "hash_algorithm"},
                message="Only hash_algorithm='sha256' is supported.",
            )
        if not isinstance(include_line_count, bool):
            return self._error(
                path=path,
                error={"type": "invalid_argument", "field": "include_line_count"},
                message="Argument 'include_line_count' must be a boolean.",
            )
        if not isinstance(follow_symlinks, bool):
            return self._error(
                path=path,
                error={"type": "invalid_argument", "field": "follow_symlinks"},
                message="Argument 'follow_symlinks' must be a boolean.",
            )

        report_path = tool_report_abspath(path)
        if not os.path.lexists(path):
            return self._result(
                ok=True,
                path=report_path,
                exists=False,
                type=None,
                is_file=False,
                is_dir=False,
                is_symlink=False,
                size_bytes=None,
                modified_time=None,
                permissions=None,
                readable=False,
                writable=False,
                line_count=None,
                encoding_guess=None,
                is_binary=None,
                sha256=None,
                hash_skipped_reason=None,
                message="Path does not exist.",
            )

        is_symlink = os.path.islink(path)
        stat_path = os.path.realpath(path) if follow_symlinks and is_symlink else path
        try:
            st = os.stat(stat_path, follow_symlinks=follow_symlinks)
        except OSError as e:
            return self._error(
                path=path,
                error={"type": "stat_failed", "message": str(e)},
                message="Failed to stat path.",
            )

        is_dir = stat_module.S_ISDIR(st.st_mode)
        is_file = stat_module.S_ISREG(st.st_mode)
        if is_symlink and not follow_symlinks:
            entry_type = "symlink"
        elif is_dir:
            entry_type = "directory"
        elif is_file:
            entry_type = "file"
        else:
            entry_type = "other"

        readable = os.access(stat_path, os.R_OK)
        writable = os.access(stat_path, os.W_OK)
        size_bytes = st.st_size
        permissions = self._format_permissions(st.st_mode)
        modified_time = self._mtime_iso(st)

        line_count: Optional[int] = None
        encoding_guess: Optional[str] = None
        is_binary: Optional[bool] = None
        sha256: Optional[str] = None
        hash_skipped_reason: Optional[str] = None

        if is_file and readable:
            is_binary = guess_is_binary(stat_path)
            encoding_guess = None if is_binary else "utf-8"

            if include_line_count and not is_binary:
                try:
                    line_count = count_lines_in_text(read_text_for_edit(stat_path))
                except OSError as e:
                    return self._error(
                        path=path,
                        error={"type": "read_failed", "message": str(e)},
                        message="Failed to read file for line count.",
                    )

            if include_hash:
                if size_bytes > self.max_hash_bytes:
                    hash_skipped_reason = "file_too_large"
                elif is_binary:
                    hash_skipped_reason = "binary_file"
                else:
                    try:
                        sha256 = sha256_hex_of_file(stat_path)
                    except OSError as e:
                        return self._error(
                            path=path,
                            error={"type": "read_failed", "message": str(e)},
                            message="Failed to read file for hash.",
                        )

        return self._result(
            ok=True,
            path=report_path,
            exists=True,
            type=entry_type,
            is_file=is_file,
            is_dir=is_dir,
            is_symlink=is_symlink,
            size_bytes=size_bytes,
            modified_time=modified_time,
            permissions=permissions,
            readable=readable,
            writable=writable,
            line_count=line_count,
            encoding_guess=encoding_guess,
            is_binary=is_binary,
            sha256=sha256,
            hash_skipped_reason=hash_skipped_reason,
            message="File info collected.",
        )
