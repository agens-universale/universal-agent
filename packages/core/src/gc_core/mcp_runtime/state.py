"""Состояние установленного MCP-runtime (после gc-mcp-install / install.py)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gc_core.mcp_runtime.paths import mcp_bin_dir, mcp_env_root, runtime_state_path


@dataclass(frozen=True)
class McpRuntimeState:
    venv_dir: Path
    bin_dir: Path
    manifest_path: Path | None
    updated_at: str


def save_runtime_state(*, manifest_path: Path | None) -> McpRuntimeState:
    venv_dir = mcp_env_root()
    bin_dir = mcp_bin_dir()
    state = McpRuntimeState(
        venv_dir=venv_dir,
        bin_dir=bin_dir,
        manifest_path=manifest_path.resolve() if manifest_path else None,
        updated_at=datetime.now(UTC).isoformat(),
    )
    path = runtime_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "venv_dir": str(state.venv_dir),
        "bin_dir": str(state.bin_dir),
        "manifest_path": str(state.manifest_path) if state.manifest_path else None,
        "updated_at": state.updated_at,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return state


def load_runtime_state() -> McpRuntimeState | None:
    path = runtime_state_path()
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        venv_dir = Path(str(raw["venv_dir"]))
        bin_dir = Path(str(raw["bin_dir"]))
    except KeyError:
        return None
    manifest_raw = raw.get("manifest_path")
    manifest_path = Path(str(manifest_raw)) if manifest_raw else None
    updated_at = str(raw.get("updated_at") or "")
    if not bin_dir.is_dir():
        return None
    return McpRuntimeState(
        venv_dir=venv_dir,
        bin_dir=bin_dir,
        manifest_path=manifest_path,
        updated_at=updated_at,
    )
