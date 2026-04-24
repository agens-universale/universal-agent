"""Ставит MCP из config.yml: pip install wheel / docker compose build / npx ensure."""
from __future__ import annotations

import shutil
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
    elif itype == "npx":
        _install_npx(entry.name, entry.install.commands)
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


def _ensure_node() -> str:
    """Гарантировать, что `npx` есть в PATH. Вернуть путь до npx.

    Идемпотентно: если npx уже установлен — сразу выходим. Иначе пытаемся
    поставить Node.js штатным пакетным менеджером платформы:
      - macOS : brew install node
      - Windows: winget install OpenJS.NodeJS.LTS
      - Linux  : apt-get / dnf / pacman / apk install nodejs npm (через sudo)
    Если не получилось — печатаем ссылку на nodejs.org и падаем.
    """
    npx = shutil.which("npx")
    if npx:
        print(f"[npx] already installed: {npx}")
        return npx

    print("[npx] not found in PATH — installing Node.js…")
    installers: list[list[str]] = []
    if sys.platform == "darwin":
        if shutil.which("brew"):
            installers.append(["brew", "install", "node"])
    elif sys.platform == "win32":
        if shutil.which("winget"):
            installers.append([
                "winget", "install", "-e",
                "--id", "OpenJS.NodeJS.LTS",
                "--silent",
                "--accept-source-agreements",
                "--accept-package-agreements",
            ])
    else:
        # Перебираем Linux-пакетники по порядку; первый найденный выигрывает.
        for pm, args in (
            ("apt-get", ["sudo", "apt-get", "install", "-y", "nodejs", "npm"]),
            ("dnf",     ["sudo", "dnf", "install", "-y", "nodejs", "npm"]),
            ("pacman",  ["sudo", "pacman", "-S", "--noconfirm", "nodejs", "npm"]),
            ("apk",     ["sudo", "apk", "add", "--no-cache", "nodejs", "npm"]),
        ):
            if shutil.which(pm):
                installers.append(args)
                break

    if not installers:
        raise InstallError(
            "Cannot auto-install Node.js on this platform. "
            "Install it manually from https://nodejs.org/ and re-run install.py."
        )

    for cmd in installers:
        print(f"[npx] {' '.join(cmd)}")
        rc = subprocess.call(cmd)
        if rc != 0:
            raise InstallError(
                f"Node.js installer failed (exit {rc}): {' '.join(cmd)}. "
                f"Install manually from https://nodejs.org/ and re-run."
            )

    npx = shutil.which("npx")
    if not npx:
        raise InstallError(
            "Node.js installer finished, but `npx` still not in PATH. "
            "Re-open your shell (to refresh PATH) and re-run install.py."
        )
    print(f"[npx] installed: {npx}")
    return npx


def _install_npx(name: str, commands: tuple[tuple[str, ...], ...]) -> None:
    """Установка MCP, доставляемого через npx.

    Шаги:
      1. ensure Node.js/npx (идемпотентно).
      2. прогнать install.commands из config.yml (обычно — pre-fetch npm-пакетов
         и загрузка Playwright-браузеров). Первое `npx` в argv подменяется на
         абсолютный путь, чтобы Windows .cmd-shim не требовал shell=True.
    """
    npx = _ensure_node()
    for cmd in commands:
        argv = list(cmd)
        if argv and argv[0] == "npx":
            argv[0] = npx
        print(f"[install] {name}: {' '.join(argv)}")
        subprocess.run(argv, check=True)


def main() -> int:
    root = Path(__file__).resolve().parent
    manifest = load_manifest(root / "config.yml")
    if not manifest:
        print("[info] no mcps declared in config.yml — nothing to install")
        return 0
    for entry in manifest:
        install_entry(entry, root)
    print("[done] install complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
