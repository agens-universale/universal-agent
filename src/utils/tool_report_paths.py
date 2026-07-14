"""Normalize paths in tool JSON responses."""

from __future__ import annotations

import os
from typing import Any, Optional


def tool_report_abspath(path: Optional[str], backend: Optional[Any] = None) -> Optional[str]:
    """
    Absolute path as the model should see it.

    With a sandbox backend the path belongs to the task container: resolve relatives
    against the container cwd and normalize with the backend's path semantics. Without
    a backend (local CLI) keep the host's native ``os.path.abspath``, which also
    collapses ``.`` / ``..`` so the model can reuse the string verbatim later.
    """
    if path is None:
        return None
    if not isinstance(path, str):
        return str(path)
    if not path:
        return path
    if backend is not None and getattr(backend, "is_sandbox", False):
        if not path.startswith("/"):
            path = backend.join(backend.default_cwd() or "/", path)
        return backend.normpath(path)
    return os.path.abspath(path)
