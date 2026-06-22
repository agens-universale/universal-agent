"""Harbor variant of fs_file_info.

Faithful 1:1 reimplementation of ``FileInfo.__call__`` for the External adapter:
same validation, same classification logic (inherited helpers), same ``_result``
envelope. The ONLY difference is physical I/O — file existence/stat/read/hash
go through HarborBackend (the task container) instead of the host filesystem.
The base ``@enforce_write_guard`` decorator is not applicable here (read-only tool);
no security decorator is added.
"""
import json
from datetime import datetime, timezone
from typing import Optional

from utils.file_text import (
    _classify_bytes_sample,
    count_lines_in_text,
    sha256_hex_of_text,
)
from tools.fs.file_info import FileInfo
from utils.tool_report_paths import tool_report_abspath

DEFAULT_MAX_HASH_BYTES = FileInfo.max_hash_bytes


class HarborFileInfo(FileInfo):
    def __call__(
        self,
        path: str,
        include_hash: bool = False,
        hash_algorithm: str = "sha256",
        include_line_count: bool = False,
        follow_symlinks: bool = False,
        **kwargs,
    ) -> str:
        backend = self._ctx.harbor_backend

        # --- argument validation (identical to base) ---
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

        # --- existence check (I/O swapped to container backend) ---
        if not backend.exists(path):
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
                binary_detection_reason=None,
                sha256=None,
                hash_mode=None,
                hash_skipped_reason=None,
                message="Path does not exist.",
            )

        # --- stat (I/O swapped to container backend) ---
        try:
            st = backend.stat(path)
        except OSError as e:
            return self._error(
                path=path,
                error={"type": "stat_failed", "message": str(e)},
                message="Failed to stat path.",
            )

        is_symlink = st["is_symlink"]
        is_dir = st["is_dir"]
        is_file = st["is_file"]
        size_bytes = st["size"]
        # mtime from stat dict (int epoch seconds)
        mtime_raw = st.get("mtime")
        if mtime_raw is not None:
            modified_time = datetime.fromtimestamp(int(mtime_raw), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            modified_time = None

        # permissions: stat dict returns octal string (e.g. "644"); convert to 9-char
        # symbolic form to match base FileInfo._format_permissions output ("rw-r--r--").
        raw_mode = st.get("mode")
        if raw_mode is not None:
            permissions = self._format_permissions(int(str(raw_mode), 8))
        else:
            permissions = None

        if is_symlink and not follow_symlinks:
            entry_type = "symlink"
        elif is_dir:
            entry_type = "directory"
        elif is_file:
            entry_type = "file"
        else:
            entry_type = "other"

        # Container files are always readable/writable from the backend perspective.
        readable = True
        writable = True

        line_count: Optional[int] = None
        encoding_guess: Optional[str] = None
        is_binary: Optional[bool] = None
        binary_detection_reason: Optional[str] = None
        sha256: Optional[str] = None
        hash_mode: Optional[str] = None
        hash_skipped_reason: Optional[str] = None

        if is_file and readable:
            # Read content via backend for classification
            try:
                content_str = backend.read(path)
            except OSError as e:
                return self._error(
                    path=path,
                    error={"type": "read_failed", "message": str(e)},
                    message="Failed to read file for classification.",
                )

            content_bytes = content_str.encode("utf-8")
            classification = _classify_bytes_sample(content_bytes[:8192])
            is_binary = classification.is_binary
            encoding_guess = classification.encoding_guess
            binary_detection_reason = classification.binary_detection_reason

            if include_line_count and not is_binary:
                line_count = count_lines_in_text(content_str)

            if include_hash:
                if size_bytes > self.max_hash_bytes:
                    hash_skipped_reason = "file_too_large"
                else:
                    try:
                        if is_binary:
                            # Raw-bytes hash: use backend.sha256 (sha256sum on raw bytes)
                            sha256 = backend.sha256(path)
                            hash_mode = "bytes"
                        else:
                            # Text hash: match base FileInfo.sha256_hex_of_file which
                            # opens in text mode (universal newlines: \r\n and \r → \n)
                            # with errors="ignore". Normalize line endings the same way.
                            normalized = content_str.replace("\r\n", "\n").replace("\r", "\n")
                            sha256 = sha256_hex_of_text(normalized)
                            hash_mode = "text"
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
            binary_detection_reason=binary_detection_reason,
            sha256=sha256,
            hash_mode=hash_mode,
            hash_skipped_reason=hash_skipped_reason,
            message="File info collected.",
        )
