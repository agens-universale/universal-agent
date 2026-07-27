"""Execution-environment workspace: root + I/O backend, assembled once per run."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from backends.protocol import FsBackend


@dataclass(frozen=True)
class ExecutionWorkspace:
    """Project root in the coordinates of the environment that executes tools.

    ``root`` is absolute in that environment (host path locally, container path
    in sandbox). ``backend`` is the only I/O port for facts and tool ops there.
    ``os_name`` / ``shell`` are env-block labels for that environment.
    ``root_writable`` — whether the project root accepts writes (sandbox: yes;
    local: one probe at assemble time).
    """

    root: str
    backend: FsBackend
    shell: str
    os_name: str
    root_writable: bool


def discover_backend_cwd(backend: FsBackend) -> str:
    """Working directory of the environment the backend talks to.

    ``default_cwd()`` may be None (container owns its WORKDIR); ask the
    environment itself, then fall back to ``/``.
    """
    default_cwd = getattr(backend, "default_cwd", None)
    declared = default_cwd() if callable(default_cwd) else None
    if declared:
        return declared
    exec_fn = getattr(backend, "exec", None)
    if not callable(exec_fn):
        return "/"
    try:
        res = exec_fn("pwd")
    except Exception:  # noqa: BLE001 — env may be down; callers still need a root
        return "/"
    lines = (getattr(res, "stdout", "") or "").strip().splitlines()
    if getattr(res, "return_code", 1) == 0 and lines and lines[0].startswith("/"):
        return lines[0]
    return "/"


def _config_root_raw(config: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(config, dict):
        return None
    raw = config.get("launch_root") or config.get("project_root")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def resolve_execution_root(
    backend: FsBackend,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """Resolve project root in execution-environment coordinates."""
    raw = _config_root_raw(config)
    if getattr(backend, "is_sandbox", False):
        if raw:
            # Already in container coordinates — do not host-abspath.
            norm = getattr(backend, "normpath", None)
            return norm(raw) if callable(norm) else (raw.rstrip("/") or "/")
        return discover_backend_cwd(backend)

    if raw:
        return os.path.abspath(os.path.expanduser(os.path.normpath(raw)))
    default_cwd = getattr(backend, "default_cwd", None)
    cwd = (default_cwd() if callable(default_cwd) else None) or os.getcwd()
    return os.path.abspath(cwd)


def resolve_execution_shell(backend: FsBackend) -> str:
    """Shell label for the execution environment (prompt / env-block)."""
    if getattr(backend, "is_sandbox", False):
        return "sh"
    shell = os.environ.get("SHELL", "unknown")
    if (not shell or shell == "unknown") and sys.platform == "win32":
        shell = os.environ.get("COMSPEC", "cmd.exe")
    return shell or "unknown"


def host_os_label() -> str:
    """Human-readable host OS from ``sys.platform`` (no ``platform.system()``/WMI)."""
    if sys.platform == "win32":
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("linux"):
        return "Linux"
    return sys.platform


def resolve_execution_os(backend: FsBackend) -> str:
    """OS label for the execution environment (prompt / env-block)."""
    if getattr(backend, "is_sandbox", False):
        return "Linux"
    return host_os_label()


def resolve_root_writable(backend: FsBackend, root: str) -> bool:
    """Whether ``root`` is writable in the execution environment."""
    if getattr(backend, "is_sandbox", False):
        return True
    from utils.directory_write_access import directory_allows_probe_write

    return directory_allows_probe_write(root)


def build_execution_workspace(
    backend: FsBackend,
    config: Optional[Dict[str, Any]] = None,
) -> ExecutionWorkspace:
    """Assemble workspace once (typically in ``create_run_context``)."""
    root = resolve_execution_root(backend, config)
    return ExecutionWorkspace(
        root=root,
        backend=backend,
        shell=resolve_execution_shell(backend),
        os_name=resolve_execution_os(backend),
        root_writable=resolve_root_writable(backend, root),
    )


__all__ = [
    "ExecutionWorkspace",
    "build_execution_workspace",
    "discover_backend_cwd",
    "host_os_label",
    "resolve_execution_os",
    "resolve_execution_root",
    "resolve_execution_shell",
    "resolve_root_writable",
]
