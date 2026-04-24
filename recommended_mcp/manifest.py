"""Парсер recommended_mcp/config.yml."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ManifestError(ValueError):
    """Ошибка в структуре config.yml."""


@dataclass(frozen=True)
class InstallSpec:
    type: str
    platforms: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunSpec:
    type: str
    detach: bool = False


@dataclass(frozen=True)
class MCPEntry:
    name: str
    path: Path
    install: InstallSpec
    run: RunSpec | None = None


def load_manifest(path: Path) -> list[MCPEntry]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_mcps = data.get("mcps", [])
    if not isinstance(raw_mcps, list):
        raise ManifestError(f"'mcps' must be a list in {path}")
    return [_parse_entry(m, path) for m in raw_mcps]


def _parse_entry(raw: dict[str, Any], source: Path) -> MCPEntry:
    name = raw.get("name")
    if not name:
        raise ManifestError(f"mcp entry missing 'name' in {source}")
    path_str = raw.get("path")
    if not path_str:
        raise ManifestError(f"mcp '{name}' missing 'path' in {source}")
    install_raw = raw.get("install")
    if not isinstance(install_raw, dict):
        raise ManifestError(f"mcp '{name}' missing 'install' block in {source}")
    install = InstallSpec(
        type=install_raw.get("type", ""),
        platforms=tuple(install_raw.get("platforms", ()) or ()),
    )
    if not install.type:
        raise ManifestError(f"mcp '{name}' missing 'install.type' in {source}")

    run_raw = raw.get("run")
    run: RunSpec | None = None
    if run_raw is not None:
        if not isinstance(run_raw, dict):
            raise ManifestError(f"mcp '{name}' 'run' must be a dict in {source}")
        run_type = run_raw.get("type")
        if not run_type:
            raise ManifestError(f"mcp '{name}' missing 'run.type' in {source}")
        run = RunSpec(type=run_type, detach=bool(run_raw.get("detach", False)))

    return MCPEntry(name=name, path=Path(path_str), install=install, run=run)
