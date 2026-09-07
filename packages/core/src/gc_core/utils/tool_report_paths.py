"""Normalize paths in tool JSON responses."""

from __future__ import annotations

import os
from typing import Any, Optional

from gc_core.execution_workspace import ExecutionWorkspace, build_execution_workspace


def tool_report_abspath(
    path: Optional[str],
    workspace: Optional[ExecutionWorkspace] = None,
    *,
    backend: Optional[Any] = None,
) -> Optional[str]:
    """
    Absolute path as the model should see it.

    With an ``ExecutionWorkspace`` for a sandbox, relatives resolve against
    ``workspace.root`` (same root as the env block). Local CLI keeps the host's
    native ``os.path.abspath`` (process cwd).

    ``backend=`` remains as a thin compat shim for older call sites/tests.
    """
    if path is None:
        return None
    if not isinstance(path, str):
        return str(path)
    if not path:
        return path

    ws = workspace
    if ws is None and backend is not None and getattr(backend, "is_sandbox", False):
        ws = build_execution_workspace(backend)

    if ws is not None and getattr(ws.backend, "is_sandbox", False):
        if not path.startswith("/"):
            path = ws.backend.join(ws.root, path)
        return ws.backend.normpath(path)

    return os.path.abspath(path)
