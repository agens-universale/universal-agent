from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_DEFAULT_MAILTO = "team@company.example"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    mailto: str
    ncbi_api_key: Optional[str]
    semantic_scholar_api_key: Optional[str]
    core_api_key: Optional[str]
    cache_dir: Path
    cache_enabled: bool
    http_timeout_sec: int
    queue_timeout_sec: int
    host: str
    port: int

    def keys_configured(self) -> dict[str, bool]:
        return {
            "ncbi": self.ncbi_api_key is not None,
            "semantic_scholar": self.semantic_scholar_api_key is not None,
            "core": self.core_api_key is not None,
        }

    def mailto_is_real(self) -> bool:
        return self.mailto != _DEFAULT_MAILTO


def load_config() -> Config:
    raw_cache_dir = os.environ.get("SCIENCE_MCP_CACHE_DIR")
    cache_path = (
        Path(raw_cache_dir).expanduser()
        if raw_cache_dir
        else Path.home() / ".cache" / "science-mcp"
    )
    return Config(
        mailto=os.environ.get("SCIENCE_MCP_MAILTO") or _DEFAULT_MAILTO,
        ncbi_api_key=os.environ.get("NCBI_API_KEY") or None,
        semantic_scholar_api_key=os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or None,
        core_api_key=os.environ.get("CORE_API_KEY") or None,
        cache_dir=cache_path,
        cache_enabled=_env_bool("SCIENCE_MCP_CACHE", True),
        http_timeout_sec=_env_int("SCIENCE_MCP_HTTP_TIMEOUT", 15),
        queue_timeout_sec=_env_int("SCIENCE_MCP_QUEUE_TIMEOUT", 120),
        host=os.environ.get("SCIENCE_MCP_HOST") or "127.0.0.1",
        port=_env_int("SCIENCE_MCP_PORT", 8765),
    )
