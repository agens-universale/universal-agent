"""Server configuration loaded from env vars."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType


def _parse_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"invalid boolean: {value!r}")


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


DEFAULT_TIMEOUTS: Mapping[str, float] = MappingProxyType({
    "get_state": 10.0,
    "click": 5.0,
    "type": 5.0,
    "scroll": 5.0,
    "drag": 5.0,
    "key": 5.0,
    "shortcut": 5.0,
    "resize": 5.0,
    "switch": 5.0,
    "launch_app": 15.0,
    "wait": 30.0,
    "capture_screenshot": 5.0,
    "analyze_screen_region": 5.0,
})


@dataclass(frozen=True)
class ServerConfig:
    readonly: bool = False
    enable_vision: bool = False
    disabled_tools: frozenset[str] = field(default_factory=frozenset)
    log_level: str = "INFO"
    log_dir: Path | None = None
    include_invisible_default: bool = False
    session_id: str | None = None
    state_ttl_seconds: int = 3600
    timeouts: Mapping[str, float] = field(
        default_factory=lambda: MappingProxyType(dict(DEFAULT_TIMEOUTS))
    )


def load_from_env() -> ServerConfig:
    readonly = _parse_bool(os.environ.get("COMPUTER_MCP_READONLY", "false"))
    enable_vision = _parse_bool(os.environ.get("COMPUTER_MCP_ENABLE_VISION", "false"))

    disabled_csv = os.environ.get("COMPUTER_MCP_DISABLED_TOOLS", "").strip()
    disabled_tools = (
        frozenset(t.strip() for t in disabled_csv.split(",") if t.strip())
        if disabled_csv
        else frozenset()
    )

    log_level = os.environ.get("COMPUTER_MCP_LOG_LEVEL", "INFO").upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise ValueError(f"invalid COMPUTER_MCP_LOG_LEVEL: {log_level!r}")

    log_dir_s = os.environ.get("COMPUTER_MCP_LOG_DIR")
    log_dir = Path(log_dir_s) if log_dir_s else None

    session_id = os.environ.get("COMPUTER_MCP_SESSION_ID") or None

    ttl_raw = os.environ.get("COMPUTER_MCP_STATE_TTL", "3600")
    try:
        ttl = int(ttl_raw)
    except ValueError as e:
        raise ValueError(f"invalid COMPUTER_MCP_STATE_TTL: {ttl_raw!r}") from e
    if ttl <= 0:
        raise ValueError(f"COMPUTER_MCP_STATE_TTL must be positive, got {ttl}")

    return ServerConfig(
        readonly=readonly,
        enable_vision=enable_vision,
        disabled_tools=disabled_tools,
        log_level=log_level,
        log_dir=log_dir,
        session_id=session_id,
        state_ttl_seconds=ttl,
    )
