"""Persist last UIA snapshot to %TEMP%\\computer-mcp\\ between short-lived calls."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default session identifier used when the caller hasn't provided one.
# Previously this was `os.getppid()`, but on Windows pip/uv install console
# scripts as transient `.exe` launchers that wrap python.exe multiple levels
# deep; every MCP invocation gets a different PID chain, and walking the
# ancestors via psutil can't distinguish the stable agent python.exe from
# the transient launcher's python.exe (same executable name). A fixed
# default keeps both get_state and the next click/type in the same file.
# Parallel agent sessions on the same machine should set
# `COMPUTER_MCP_SESSION_ID` explicitly (e.g. to their own PID or UUID).
_DEFAULT_SESSION_ID = "default"


class StateNotFoundError(Exception):
    """Snapshot file does not exist."""


class StateExpiredError(Exception):
    """Snapshot file is older than TTL."""


def _base_dir() -> Path:
    # Re-read env each call so tests that monkeypatch TEMP/TMP take effect
    # (tempfile.gettempdir() caches its result on first use).
    for var in ("TMPDIR", "TEMP", "TMP"):
        val = os.environ.get(var)
        if val:
            return Path(val) / "computer-mcp"
    return Path(tempfile.gettempdir()) / "computer-mcp"


def _resolve_default_session_id() -> str:
    """Return the default session id used when no explicit id is provided.

    See _DEFAULT_SESSION_ID comment for rationale.
    """
    return _DEFAULT_SESSION_ID


class StateStore:
    def __init__(self, session_id: str | None, ttl_seconds: int) -> None:
        self.session_id = (
            session_id if session_id is not None else _resolve_default_session_id()
        )
        self.ttl_seconds = ttl_seconds

    @property
    def path(self) -> Path:
        return _base_dir() / f"last_state_{self.session_id}.json"

    def save(self, snapshot: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = dict(snapshot)
        data["saved_at"] = time.time()
        tmp = self.path.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            os.replace(tmp, self.path)
            # Note: no fsync/FlushFileBuffers. On power loss the snapshot may revert
            # to the prior version or be empty. Acceptable because the state cache
            # is trivially recoverable by re-issuing get_state.
        except OSError:
            # Leave original file intact; clean temp if still exists.
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            raise StateNotFoundError(f"no snapshot at {self.path}")
        mtime = self.path.stat().st_mtime
        age = time.time() - mtime
        if age > self.ttl_seconds:
            raise StateExpiredError(f"snapshot age {age:.0f}s exceeds ttl {self.ttl_seconds}s")
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise StateNotFoundError(f"snapshot at {self.path} is corrupt: {e}") from e


def cleanup_stale(ttl_seconds: int) -> None:
    """Delete snapshot files older than ttl_seconds."""
    root = _base_dir()
    if not root.exists():
        return
    now = time.time()
    for p in root.glob("last_state_*.json"):
        try:
            if now - p.stat().st_mtime > ttl_seconds:
                p.unlink()
        except OSError:
            pass
