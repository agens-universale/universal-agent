"""Запускает MCP с блоком run: в config.yml (docker compose up).

MCP без run-блока пропускаются — они вызываются агентом по stdio on-demand.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Позволяет запускать файл напрямую: `python recommended_mcp/run.py`.
# При `python -m recommended_mcp.run` и при import из тестов — no-op.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "recommended_mcp"

from recommended_mcp.manifest import MCPEntry, load_manifest


class RunError(RuntimeError):
    """Невозможно запустить MCP."""


def run_entry(entry: MCPEntry, root: Path) -> None:
    if entry.run is None:
        print(f"[skip] {entry.name}: no run step (stdio/on-demand)")
        return

    pkg_path = (root / entry.path).resolve()
    rtype = entry.run.type
    if rtype == "docker-compose":
        _run_docker_compose(entry, pkg_path)
    else:
        raise RunError(
            f"mcp {entry.name!r} uses run.type={rtype!r} which is not supported "
            f"by this run.py. Update run.py to handle {rtype!r}, "
            f"or remove the run: block in config.yml."
        )


def _run_docker_compose(entry: MCPEntry, pkg_path: Path) -> None:
    compose = pkg_path / "docker-compose.yml"
    if not compose.exists():
        raise RunError(f"mcp {entry.name!r}: docker-compose.yml not found at {compose}")
    cmd = ["docker", "compose", "-f", str(compose), "up"]
    if entry.run and entry.run.detach:
        cmd.append("-d")
    print(f"[run] {entry.name}: {' '.join(cmd[-2:])}")
    subprocess.run(cmd, check=True)


def main() -> int:
    root = Path(__file__).resolve().parent
    manifest = load_manifest(root / "config.yml")
    if not manifest:
        print("[info] no mcps declared in config.yml — nothing to run")
        return 0
    for entry in manifest:
        run_entry(entry, root)
    print("[done] run complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
