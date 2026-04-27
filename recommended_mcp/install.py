"""Ставит MCP из config.yml: pip install wheel / docker compose build / npx ensure."""
from __future__ import annotations

import os
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


# Pinned Node.js версия для Windows-fallback (если winget недоступен).
# LTS, x64. Обновлять здесь при необходимости.
_NODE_WIN_MSI_URL = "https://nodejs.org/dist/v20.18.0/node-v20.18.0-x64.msi"


def _download_url_windows(url: str, dest: str) -> None:
    """Скачать URL в файл на Windows. Сначала urllib, fallback на PowerShell.

    urllib работает на нормальном Python (CA-bundle через системный store).
    На embeddable Python без CA или на Windows-машинах с MITM-прокси /
    устаревшим cert-store urllib падает на SSL handshake. PowerShell
    Invoke-WebRequest использует Windows-store сертификатов и обычно
    срабатывает там, где urllib не смог.
    """
    import urllib.error
    import urllib.request

    try:
        urllib.request.urlretrieve(url, dest)
        return
    except (urllib.error.URLError, OSError) as e:
        print(f"[download] urllib failed ({e}); retrying via PowerShell")

    rc = subprocess.call([
        "powershell", "-NoProfile", "-Command",
        f"$ProgressPreference='SilentlyContinue'; "
        f"Invoke-WebRequest -Uri '{url}' -OutFile '{dest}'"
    ])
    if rc != 0:
        raise InstallError(
            f"Download failed for {url} (PowerShell exit {rc}). "
            f"Check network access or install Node.js manually from https://nodejs.org/."
        )


def _refresh_windows_path() -> None:
    """Перечитать PATH из Windows-реестра в os.environ текущего процесса.

    Windows-инсталляторы (winget, msiexec) пишут PATH в registry, но
    активные процессы держат снимок переменных окружения с момента старта.
    Без обновления `shutil.which` после установки не находит свеже-
    поставленные программы — отсюда и берётся «still not in PATH», даже
    если установка прошла успешно. Эта функция читает Machine + User scope
    из реестра и переписывает os.environ["PATH"] в текущем процессе.

    No-op на не-Windows платформах.
    """
    if sys.platform != "win32":
        return
    import winreg

    parts: list[str] = []
    for hive, key in [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    ]:
        try:
            with winreg.OpenKey(hive, key) as k:
                value, _ = winreg.QueryValueEx(k, "Path")
                if value:
                    parts.append(value)
        except OSError:
            pass

    if parts:
        os.environ["PATH"] = ";".join(parts)


def _install_node_msi_windows() -> None:
    """Fallback: скачать Node.js MSI с nodejs.org и поставить через msiexec /qn.

    Нужен, когда winget недоступен (Windows Sandbox, Windows Server без App
    Installer, корпоративные образы с вырезанным Microsoft Store). MSI пишет
    в machine PATH — после установки делаем _refresh_windows_path, чтобы
    текущий процесс видел новый PATH без перезапуска shell.
    """
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".msi", delete=False)
    tmp.close()
    print(f"[npx] downloading Node.js MSI from {_NODE_WIN_MSI_URL}")
    _download_url_windows(_NODE_WIN_MSI_URL, tmp.name)
    print(f"[npx] msiexec /i {tmp.name} /qn")
    rc = subprocess.call(["msiexec", "/i", tmp.name, "/qn"])
    if rc != 0:
        raise InstallError(
            f"msiexec failed (exit {rc}) for {tmp.name}. "
            f"Install Node.js manually from https://nodejs.org/ and re-run."
        )


def _ensure_node() -> str:
    """Гарантировать, что `npx` есть в PATH. Вернуть путь до npx.

    Идемпотентно: если npx уже установлен — сразу выходим. Иначе пытаемся
    поставить Node.js штатным пакетным менеджером платформы:
      - macOS : brew install node
      - Windows: winget → MSI с nodejs.org (fallback, если winget нет)
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
            # Fallback для окружений без App Installer (Sandbox, Server,
            # корпоративные образы). Прямая загрузка MSI с nodejs.org.
            _install_node_msi_windows()
            _refresh_windows_path()
            npx = shutil.which("npx")
            if not npx:
                raise InstallError(
                    "Node.js MSI installer finished, but `npx` still not in PATH. "
                    "Re-open your shell (to refresh PATH) and re-run install.py."
                )
            print(f"[npx] installed: {npx}")
            return npx
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

    _refresh_windows_path()
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
         и загрузка Playwright-браузеров). Первый элемент argv (npm/npx/yarn/...)
         резолвится через shutil.which — на Windows .cmd-shim'ы CreateProcess
         без shell=True не находит по bare-имени, нужен абсолютный путь.
    """
    _ensure_node()
    for cmd in commands:
        argv = list(cmd)
        if argv:
            resolved = shutil.which(argv[0])
            if resolved:
                argv[0] = resolved
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
