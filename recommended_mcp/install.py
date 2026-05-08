"""Ставит MCP из config.yml: pip install wheel / docker compose build."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Позволяет запускать файл напрямую: `python recommended_mcp/install.py`.
# При `python -m recommended_mcp.install` и при import из тестов — no-op.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "recommended_mcp"

from recommended_mcp.manifest import MCPEntry, load_manifest


class InstallError(RuntimeError):
    """Невозможно выполнить установку MCP."""


def install_entry(entry: MCPEntry, root: Path) -> None:
    """Установить один MCP. root — каталог, относительно которого резолвится entry.path."""
    if entry.install.platforms and sys.platform not in entry.install.platforms:
        print(
            f"[skip] {entry.name}: platform {sys.platform!r} not in "
            f"{list(entry.install.platforms)}"
        )
        return

    pkg_path = (root / entry.path).resolve()
    itype = entry.install.type

    if itype == "wheel":
        _install_wheel(entry.name, pkg_path)
    elif itype == "docker":
        _install_docker(entry.name, pkg_path)
    elif itype == "source":
        _install_source(entry.name, entry.install.source, root, editable=False)
    else:
        raise InstallError(
            f"mcp {entry.name!r} uses install.type={itype!r} which is not supported "
            f"by this install.py. Update install.py to handle {itype!r}, "
            f"or comment out the block in config.yml."
        )


def _install_wheel(name: str, pkg_path: Path) -> None:
    dist = pkg_path / "dist"
    wheels = sorted(dist.glob("*.whl"))
    if not wheels:
        raise InstallError(f"mcp {name!r}: no wheel found in {dist}")
    if len(wheels) > 1:
        raise InstallError(
            f"mcp {name!r}: multiple wheels in {dist}: {[w.name for w in wheels]}. "
            f"Keep only one."
        )
    wheel = wheels[0]
    print(f"[install] {name}: pip install {wheel.name}")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", str(wheel)],
        check=True,
    )


def _install_docker(name: str, pkg_path: Path) -> None:
    compose = pkg_path / "docker-compose.yml"
    if not compose.exists():
        raise InstallError(f"mcp {name!r}: docker-compose.yml not found at {compose}")
    print(f"[install] {name}: docker compose build")
    subprocess.run(
        ["docker", "compose", "-f", str(compose), "build"],
        check=True,
    )


def _install_source(
    name: str,
    source: Path | None,
    root: Path,
    *,
    editable: bool,
) -> None:
    if source is None:
        raise InstallError(
            f"mcp {name!r}: install.type=source requires install.source"
        )
    src_path = (root / source).resolve()
    if not (src_path / "pyproject.toml").exists():
        raise InstallError(
            f"mcp {name!r}: pyproject.toml not found in {src_path} "
            f"(install.source must point to a Python package root)"
        )
    cmd = [sys.executable, "-m", "pip", "install"]
    if editable:
        cmd.append("-e")
    cmd.append(str(src_path))
    mode = "pip install -e " if editable else "pip install "
    print(f"[install] {name}: {mode}{src_path}")
    subprocess.run(cmd, check=True)


def install_source_entry(entry: MCPEntry, root: Path, *, editable: bool) -> None:
    """Публичная обёртка с editable-флагом.

    install_entry() делает editable=False; этот враппер используется из main()
    при --editable и из тестов.
    """
    if entry.install.platforms and sys.platform not in entry.install.platforms:
        print(
            f"[skip] {entry.name}: platform {sys.platform!r} not in "
            f"{list(entry.install.platforms)}"
        )
        return
    if entry.install.type != "source":
        raise InstallError(
            f"install_source_entry called for non-source mcp {entry.name!r} "
            f"(type={entry.install.type!r})"
        )
    _install_source(entry.name, entry.install.source, root, editable=editable)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Install MCPs from recommended_mcp/config.yml",
    )
    parser.add_argument(
        "-e", "--editable",
        action="store_true",
        help="Install source-type MCPs in editable mode (pip install -e). "
             "Применяется только к install.type=source. "
             "Для wheel/docker/npx игнорируется.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    manifest = load_manifest(root / "config.yml")
    if not manifest:
        print("[info] no mcps declared in config.yml — nothing to install")
        return 0
    for entry in manifest:
        if entry.install.type == "source" and args.editable:
            install_source_entry(entry, root, editable=True)
        else:
            install_entry(entry, root)
    print("[done] install complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
