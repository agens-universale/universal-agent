"""PATH и резолв команд MCP для subprocess агента."""

from __future__ import annotations

import os
import shutil
from typing import Mapping

from gc_core.mcp_runtime.paths import mcp_bin_dir
from gc_core.mcp_runtime.state import load_runtime_state


def _effective_bin_dir() -> os.PathLike[str] | None:
    state = load_runtime_state()
    if state is not None and state.bin_dir.is_dir():
        return state.bin_dir
    fallback = mcp_bin_dir()
    return fallback if fallback.is_dir() else None


def augment_process_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Prepend MCP bin в PATH дочернего процесса (не трогая env агента)."""
    merged = dict(os.environ if env is None else env)
    bin_dir = _effective_bin_dir()
    if bin_dir is None:
        return merged
    bin_path = os.fspath(bin_dir)
    current = merged.get("PATH", "")
    if bin_path not in current.split(os.pathsep):
        merged["PATH"] = bin_path + (os.pathsep + current if current else "")
    return merged


def mcp_stdio_env(server_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Env для stdio MCP: mcp-env PATH + per-server overrides из config."""
    merged = augment_process_env()
    if server_env:
        merged.update({str(k): str(v) for k, v in server_env.items()})
    return merged


def resolve_mcp_command(command: str) -> str:
    """Найти MCP-команду в mcp-env; иначе вернуть command как есть."""
    command = os.path.expanduser(command)
    bin_dir = _effective_bin_dir()
    if bin_dir is not None:
        found = shutil.which(command, path=os.fspath(bin_dir))
        if found:
            return found
    if os.name != "nt":
        return command
    if os.path.splitext(command)[1]:
        return command
    resolved = shutil.which(command)
    return resolved if resolved else command
