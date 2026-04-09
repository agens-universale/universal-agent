"""Normalize paths in tool JSON responses."""

from __future__ import annotations

import os


def tool_report_abspath(path: str | None) -> str | None:
    """
    Absolute path in the host OS’s native form (separators, drive letters on Windows).

    Callers may already pass an absolute path; ``os.path.abspath`` still helps by
    collapsing ``.`` / ``..`` and redundant separators so the model can reuse the
    string verbatim in later tool calls on the same machine.
    """
    if path is None:
        return None
    if not isinstance(path, str):
        return str(path)
    if not path:
        return path
    return os.path.abspath(path)
