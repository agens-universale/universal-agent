import json
import stat as stat_module
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from utils.file_text import (
    BINARY_SAMPLE_BYTES,
    classify_content_from_sample,
    count_lines_in_text,
    guess_encoding,
)
from security.read_guard import enforce_read_guard
from tools.simple_tool_base import AbstractSimpleTool
from utils.tool_report_paths import tool_report_abspath

DEFAULT_MAX_HASH_BYTES = 100 * 1024 * 1024


class FileInfo(AbstractSimpleTool):
    max_hash_bytes = DEFAULT_MAX_HASH_BYTES

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "fs_file_info",
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
            "Tool name: fs_file_info\n"
            "Cheap read-only metadata before editing a file.\n"
            "Use include_hash=true to get sha256 for expected_file_sha256 in fs_search_and_replace / fs_apply_file_edits.\n"
            "Does not return full file content — use fs_read_file for that.\n"
        )

    @staticmethod
    def _result(**fields: Any) -> str:
        payload = {"tool": FileInfo.registered_tool_name(), "error": None, **fields}
        return json.dumps(payload, ensure_ascii=False)

    def _error(self, path: str, error: Dict[str, Any], message: str) -> str:
        return json.dumps(
            {
                "tool": FileInfo.registered_tool_name(),
                "ok": False,
                "path": tool_report_abspath(path, workspace=self._workspace),
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

    def _read_guard_blocked_result(
        self,
        error: Dict[str, Any],
        path: str,
        include_hash: bool = False,
        hash_algorithm: str = "sha256",
        include_line_count: bool = False,
        follow_symlinks: bool = False,
        **kwargs,
    ) -> str:
        return self._error(
            path=path,
            error=error,
            message=str(error.get("message") or "Read operation is outside allowed read_guard perimeter."),
        )

    @enforce_read_guard(path_arg="path")
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

        report_path = tool_report_abspath(path, workspace=self._workspace)
        if not self._backend.lexists(path):
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
                encoding_confidence=None,
                is_binary=None,
                binary_detection_reason=None,
                sha256=None,
                hash_mode=None,
                hash_skipped_reason=None,
                message="Path does not exist.",
            )

        is_symlink = self._backend.is_symlink(path)
        stat_path = self._backend.realpath(path) if follow_symlinks and is_symlink else path
        try:
            info = self._backend.stat_info(stat_path, follow_symlinks=follow_symlinks)
        except OSError as e:
            return self._error(
                path=path,
                error={"type": "stat_failed", "message": str(e)},
                message="Failed to stat path.",
            )

        is_dir = info["is_dir"]
        is_file = info["is_file"]
        if is_symlink and not follow_symlinks:
            entry_type = "symlink"
        elif is_dir:
            entry_type = "directory"
        elif is_file:
            entry_type = "file"
        else:
            entry_type = "other"

        readable = self._backend.readable(stat_path)
        writable = self._backend.writable(stat_path)
        size_bytes = info["size"]
        permissions = self._format_permissions(info["mode"]) if info["mode"] is not None else None
        modified_time = (
            datetime.fromtimestamp(info["mtime"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if info["mtime"] is not None else None
        )

        line_count: Optional[int] = None
        encoding_guess: Optional[str] = None
        encoding_confidence: Optional[float] = None
        is_binary: Optional[bool] = None
        binary_detection_reason: Optional[str] = None
        sha256: Optional[str] = None
        hash_mode: Optional[str] = None
        hash_skipped_reason: Optional[str] = None

        if is_file and readable:
            sample = self._backend.read_head_bytes(stat_path, BINARY_SAMPLE_BYTES)
            classification = classify_content_from_sample(stat_path, sample)
            is_binary = classification.is_binary
            binary_detection_reason = classification.binary_detection_reason
            if is_binary:
                encoding_guess = None
                encoding_confidence = None
            elif classification.encoding_guess is not None:
                encoding_guess = classification.encoding_guess
                encoding_confidence = 1.0
            else:
                encoding_guess, encoding_confidence = guess_encoding(sample)

            if include_line_count and not is_binary:
                try:
                    line_count = count_lines_in_text(self._backend.read(stat_path))
                except UnicodeDecodeError:
                    line_count = None
                except OSError as e:
                    return self._error(
                        path=path,
                        error={"type": "read_failed", "message": str(e)},
                        message="Failed to read file for line count.",
                    )

            if include_hash:
                if size_bytes > self.max_hash_bytes:
                    hash_skipped_reason = "file_too_large"
                else:
                    try:
                        sha256 = self._backend.sha256_bytes(stat_path)
                        hash_mode = "bytes"
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
            encoding_confidence=encoding_confidence,
            is_binary=is_binary,
            binary_detection_reason=binary_detection_reason,
            sha256=sha256,
            hash_mode=hash_mode,
            hash_skipped_reason=hash_skipped_reason,
            message="File info collected.",
        )
