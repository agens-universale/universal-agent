"""Shared path-kind error payloads for directory-only fs tools."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from utils.tool_report_paths import tool_report_abspath


def not_a_directory_error(
    path: str,
    *,
    backend: Any,
    workspace: Optional[Any] = None,
    contract: str,
) -> Dict[str, Any]:
    """Build ``not_a_directory`` error with message/suggestion (no other tool names).

    ``contract`` — this tool's own role, e.g. ``"this tool searches under a directory"``.
    """
    is_file = bool(getattr(backend, "is_file", lambda _p: False)(path))
    path_kind = "file" if is_file else "other"
    try:
        normalized = backend.normpath(path) if hasattr(backend, "normpath") else path
    except Exception:
        normalized = path
    parent_raw = os.path.dirname(normalized.rstrip("/\\")) or os.path.dirname(path) or ""
    parent = (
        tool_report_abspath(parent_raw, workspace=workspace)
        if parent_raw
        else None
    )

    if path_kind == "file":
        message = f"path points to a file, not a directory; {contract}"
        suggestion = "Pass a directory path (e.g. the parent of this file)"
    else:
        message = f"path is not a directory; {contract}"
        suggestion = "Pass a directory path"

    error: Dict[str, Any] = {
        "type": "not_a_directory",
        "message": message,
        "suggestion": suggestion,
        "path_kind": path_kind,
    }
    if parent:
        error["parent"] = parent
    return error
