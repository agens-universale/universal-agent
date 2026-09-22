"""Shared FS load-size limits for tools that lift file content into memory.

Two knobs (not one):
- **read** — ``fs_read_file`` (content surfaces to the model).
- **mutate** — ``fs_apply_file_edits`` / ``fs_search_and_replace`` (full-file load).

``fs_append_file`` is not under mutate: it checks a head sample and appends.

Config (``fs_tools`` block, also flattened into ``config_dict``):
- ``max_read_bytes`` (default 8 MiB)
- ``max_mutate_bytes`` (default 16 MiB)
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

DEFAULT_FS_READ_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_FS_MUTATE_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_FS_FIND_IN_FILES_MAX_FILE_BYTES = 16 * 1024 * 1024


def _positive_int(raw: Any, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _fs_tools_section(config: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    raw = (config or {}).get("fs_tools")
    return raw if isinstance(raw, Mapping) else {}


def fs_read_max_bytes(config: Optional[Mapping[str, Any]] = None) -> int:
    """Max on-disk size for full-file read / content search."""
    fs = _fs_tools_section(config)
    if "max_read_bytes" in fs:
        return _positive_int(fs.get("max_read_bytes"), DEFAULT_FS_READ_MAX_BYTES)
    return DEFAULT_FS_READ_MAX_BYTES


def fs_mutate_max_bytes(config: Optional[Mapping[str, Any]] = None) -> int:
    """Max on-disk size before mutate tools load the file into memory."""
    fs = _fs_tools_section(config)
    if "max_mutate_bytes" in fs:
        return _positive_int(fs.get("max_mutate_bytes"), DEFAULT_FS_MUTATE_MAX_BYTES)
    return DEFAULT_FS_MUTATE_MAX_BYTES


def _find_in_files_section(config: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    fs = _fs_tools_section(config)
    raw = fs.get("find_in_files")
    return raw if isinstance(raw, Mapping) else {}


def fs_find_in_files_max_bytes(config: Optional[Mapping[str, Any]] = None) -> int:
    """Unified file-size cap for ``fs_find_in_files`` (file or directory mode).

    Reads ``fs_tools.find_in_files.max_file_bytes`` (default 16 MiB). Does NOT
    fall back to ``max_read_bytes`` / ``max_mutate_bytes`` — find_in_files has its
    own cap per spec.
    """
    fif = _find_in_files_section(config)
    if "max_file_bytes" in fif:
        return _positive_int(fif.get("max_file_bytes"), DEFAULT_FS_FIND_IN_FILES_MAX_FILE_BYTES)
    return DEFAULT_FS_FIND_IN_FILES_MAX_FILE_BYTES


def file_too_large_error(
    *,
    actual_bytes: int,
    max_bytes: int,
    operation: str | None = None,
) -> dict[str, Any]:
    if operation:
        message = f"File is too large for {operation}: {actual_bytes} bytes (max {max_bytes} bytes)."
    else:
        message = f"File is {actual_bytes} bytes; limit is {max_bytes} bytes."
    return {
        "type": "file_too_large",
        "actual_bytes": actual_bytes,
        "max_bytes": max_bytes,
        "message": message,
    }


def check_file_size(
    size_bytes: int,
    max_bytes: int,
    *,
    operation: str | None = None,
) -> Optional[dict[str, Any]]:
    """Return ``file_too_large`` error dict if over cap, else ``None``."""
    if size_bytes > max_bytes:
        return file_too_large_error(
            actual_bytes=size_bytes,
            max_bytes=max_bytes,
            operation=operation,
        )
    return None
