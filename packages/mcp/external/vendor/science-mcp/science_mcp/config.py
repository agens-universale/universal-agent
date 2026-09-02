from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

_DEFAULT_MAILTO = "team@company.example"

# YAML keys mapped 1:1 onto Config fields. Listed explicitly so typos in
# YAML explode loudly instead of being silently ignored.
_YAML_KEYS = frozenset({
    "mailto",
    "openalex_api_key",
    "ncbi_api_key",
    "semantic_scholar_api_key",
    "core_api_key",
    "cache_dir",
    "cache_enabled",
    "http_timeout_sec",
    "queue_timeout_sec",
    "host",
    "port",
})

def _default_search_paths() -> tuple[Path, ...]:
    return (
        Path.cwd() / "science-mcp.yaml",
        Path.home() / ".config" / "science-mcp.yaml",
    )


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
    openalex_api_key: Optional[str]
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
            "openalex": self.openalex_api_key is not None,
            "ncbi": self.ncbi_api_key is not None,
            "semantic_scholar": self.semantic_scholar_api_key is not None,
            "core": self.core_api_key is not None,
        }

    def mailto_is_real(self) -> bool:
        return self.mailto != _DEFAULT_MAILTO


def _read_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"science-mcp config at {path} must be a YAML mapping at top level")
    unknown = set(data.keys()) - _YAML_KEYS
    if unknown:
        raise ValueError(
            f"unknown keys in {path}: {sorted(unknown)}. Allowed: {sorted(_YAML_KEYS)}"
        )
    return data


def _resolve_yaml_source(yaml_path: Optional[Path]) -> dict[str, Any]:
    if yaml_path is not None:
        if not yaml_path.exists():
            raise FileNotFoundError(f"science-mcp config not found: {yaml_path}")
        return _read_yaml(yaml_path)
    for candidate in _default_search_paths():
        if candidate.exists():
            return _read_yaml(candidate)
    return {}


def load_config(yaml_path: Optional[Path] = None) -> Config:
    """Build runtime Config from YAML (if present) + env fallback.

    Lookup order per field: YAML key > env var > built-in default.
    YAML lookup order when ``yaml_path`` is not given: ./science-mcp.yaml,
    ~/.config/science-mcp.yaml.
    """
    y = _resolve_yaml_source(yaml_path)

    raw_cache_dir = y.get("cache_dir") or os.environ.get("SCIENCE_MCP_CACHE_DIR")
    cache_path = (
        Path(raw_cache_dir).expanduser()
        if raw_cache_dir
        else Path.home() / ".cache" / "science-mcp"
    )

    return Config(
        mailto=y.get("mailto") or os.environ.get("SCIENCE_MCP_MAILTO") or _DEFAULT_MAILTO,
        openalex_api_key=y.get("openalex_api_key") or os.environ.get("OPENALEX_API_KEY") or None,
        ncbi_api_key=y.get("ncbi_api_key") or os.environ.get("NCBI_API_KEY") or None,
        semantic_scholar_api_key=(
            y.get("semantic_scholar_api_key")
            or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
            or None
        ),
        core_api_key=y.get("core_api_key") or os.environ.get("CORE_API_KEY") or None,
        cache_dir=cache_path,
        cache_enabled=(
            bool(y["cache_enabled"])
            if "cache_enabled" in y
            else _env_bool("SCIENCE_MCP_CACHE", True)
        ),
        http_timeout_sec=int(
            y.get("http_timeout_sec")
            if y.get("http_timeout_sec") is not None
            else _env_int("SCIENCE_MCP_HTTP_TIMEOUT", 15)
        ),
        queue_timeout_sec=int(
            y.get("queue_timeout_sec")
            if y.get("queue_timeout_sec") is not None
            else _env_int("SCIENCE_MCP_QUEUE_TIMEOUT", 120)
        ),
        host=y.get("host") or os.environ.get("SCIENCE_MCP_HOST") or "127.0.0.1",
        port=int(
            y.get("port")
            if y.get("port") is not None
            else _env_int("SCIENCE_MCP_PORT", 8765)
        ),
    )
