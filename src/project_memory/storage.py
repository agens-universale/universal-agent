"""Project-scoped long memory file I/O. Utility layer.

No knowledge of state, ctx, prompts, backend. Path is given by caller.
All operations serialized in-process via threading.Lock; cross-process
correctness is on the atomic os.replace.
"""
from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from config_modules.project_memory import DEFAULT_PROJECT_MEMORY_MAX_CHARS

DEFAULT_MAX_CHARS: int = DEFAULT_PROJECT_MEMORY_MAX_CHARS
MAX_CHARS: int = DEFAULT_MAX_CHARS

_lock = threading.Lock()


@dataclass(frozen=True)
class ProjectMemoryStat:
    exists: bool
    chars: int
    lines: int


class ProjectMemoryError(Exception):
    """Raised by storage write/append on validation failures.

    `code` is a stable string id ("limit_exceeded", "empty_content", "io_error")
    that the controller maps to JSON `error.type` in tool responses.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def stat(path: Path) -> ProjectMemoryStat:
    """Return existence + char/line counts of the long-memory file at `path`.

    Returns `ProjectMemoryStat(exists=False, chars=0, lines=0)` if the file is missing
    or unreadable. Other readers/writers in this process won't observe partial state
    because the operation runs under `_lock`.
    """
    with _lock:
        if not path.is_file():
            return ProjectMemoryStat(exists=False, chars=0, lines=0)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ProjectMemoryStat(exists=False, chars=0, lines=0)
        return ProjectMemoryStat(
            exists=True,
            chars=len(text),
            lines=_count_lines(text),
        )


def content_hash(path: Path) -> str | None:
    """SHA-256 hex digest of file content, or ``None`` if missing/empty/whitespace-only."""
    text = read(path)
    if not text.strip():
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read(path: Path) -> str:
    """Return the full file content as text, or "" if the file is missing or unreadable.

    Symmetric with `stat()` for the missing/unreadable case: callers can rely on
    "missing or unreadable" producing an empty string, never an exception.
    Other I/O failures (mid-read errors) are not raised either — callers see "".
    """
    with _lock:
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""


def _atomic_write(path: Path, content: str) -> None:
    """Write to <path>.tmp in the same directory, then os.replace.

    Caller holds `_lock`. Creates parent dirs if missing. Survives mid-write
    crashes: a power loss leaves either the previous content or the new one,
    never a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def append(
    path: Path,
    delta: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> ProjectMemoryStat:
    """Append `delta` (stripped) to the long-memory file separated by a blank line.

    Result format: ``existing.strip() + "\\n\\n" + delta.strip()``. If the file
    is missing or empty, the result is just the stripped delta. Read-modify-write
    runs under `_lock` so concurrent appends from multiple threads do not lose data.

    Raises ProjectMemoryError("empty_content") if `delta` is empty/whitespace.
    Raises ProjectMemoryError("limit_exceeded") when the resulting size would exceed
    MAX_CHARS; in that case the file on disk is left unchanged.
    """
    if not isinstance(delta, str) or not delta.strip():
        raise ProjectMemoryError("empty_content", "delta must be non-empty for append.")
    with _lock:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        existing_stripped = existing.strip()
        delta_stripped = delta.strip()
        new_text = (
            f"{existing_stripped}\n\n{delta_stripped}"
            if existing_stripped
            else delta_stripped
        )
        if len(new_text) > max_chars:
            raise ProjectMemoryError(
                "limit_exceeded",
                f"resulting length {len(new_text)} exceeds limit {max_chars}.",
            )
        _atomic_write(path, new_text)
    return ProjectMemoryStat(
        exists=True,
        chars=len(new_text),
        lines=_count_lines(new_text),
    )


def write(
    path: Path,
    content: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> ProjectMemoryStat:
    """Atomically replace the long-memory file with `content` (stripped).

    Raises ProjectMemoryError(code="empty_content") if content is empty/whitespace.
    Raises ProjectMemoryError(code="limit_exceeded") if stripped content > MAX_CHARS;
    in this case the file on disk is left unchanged.
    """
    if not isinstance(content, str) or not content.strip():
        raise ProjectMemoryError("empty_content", "content must be non-empty for write.")
    new_text = content.strip()
    if len(new_text) > max_chars:
        raise ProjectMemoryError(
            "limit_exceeded",
            f"content length {len(new_text)} exceeds limit {max_chars}.",
        )
    with _lock:
        _atomic_write(path, new_text)
    return ProjectMemoryStat(
        exists=True,
        chars=len(new_text),
        lines=_count_lines(new_text),
    )
