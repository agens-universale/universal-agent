"""Ставит MCP из config.yml: pip install wheel / pypi / docker compose build."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from itertools import takewhile
from pathlib import Path
from typing import Optional, Tuple
# Позволяет запускать файл напрямую: `python recommended_mcp/install.py`.
# При `python -m recommended_mcp.install` и при import из тестов — no-op.
if __package__ in (None, ""):
    _repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_repo_root / "src"))
    sys.path.insert(0, str(_repo_root))
    __package__ = "recommended_mcp"

from mcp_runtime.manifest_resolve import recipe_root, resolve_install_manifest_path
from mcp_runtime.state import save_runtime_state
from mcp_runtime.venv import (
    active_pip_python,
    ensure_mcp_venv,
    pip_argv,
    pip_install_argv,
    pip_install_target,
    query_installed_version,
)
from recommended_mcp.manifest import (
    MCPEntry,
    load_agent_dep_dir,
    load_manifest,
)

NODE_VERSION = "18.0.0"
_NODE_DIST_BASE = "https://nodejs.org/dist"


class InstallError(RuntimeError):
    """Невозможно выполнить установку MCP."""


def _platform_supported(entry: MCPEntry) -> bool:
    return not entry.install.platforms or sys.platform in entry.install.platforms


def _short_reason(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return f"pip завершился с кодом {exc.returncode}"
    return str(exc) or exc.__class__.__name__


def run_install_entries(manifest, root: Path, agent_dep_dir, *, editable: bool):
    """Установить все рецепты с изоляцией падений. Возвращает список
    (name, status, detail), status ∈ {installed, skipped, failed}."""
    results: list[tuple[str, str, str]] = []
    for entry in manifest:
        if not _platform_supported(entry):
            results.append((entry.name, "skipped", f"platform {sys.platform}"))
            continue
        try:
            if entry.install.type == "source" and editable:
                install_source_entry(entry, root, editable=True)
            else:
                install_entry(entry, root, agent_dep_dir)
            results.append((entry.name, "installed", ""))
        except (subprocess.CalledProcessError, InstallError, OSError) as exc:
            reason = _short_reason(exc)
            results.append((entry.name, "failed", reason))
            print(f"[warn] {entry.name}: пропущен — {reason}")
    return results


def find_entry(manifest, name: str):
    """Найти запись манифеста по имени; None если нет."""
    for entry in manifest:
        if entry.name == name:
            return entry
    return None


def _dist_name(entry: MCPEntry, root: Path = Path(".")) -> str | None:
    """Имя дистрибутива для `pip uninstall`. pypi → из package-спеки;
    source → [project].name из pyproject; wheel → из имени .whl."""
    itype = entry.install.type
    if itype == "pypi" and entry.install.package:
        return _pypi_distribution_name(entry.install.package)
    if itype == "source" and entry.install.source is not None:
        pyproject = (root / entry.install.source).resolve() / "pyproject.toml"
        if pyproject.is_file():
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            name = (data.get("project") or {}).get("name")
            return str(name) if name else None
        return None
    if itype == "wheel":
        pkg_path = (root / entry.path).resolve() / "dist"
        wheels = sorted(pkg_path.glob("*.whl"))
        if wheels:
            parts = wheels[0].stem.split("-")
            return "-".join(takewhile(lambda p: not (p and p[0].isdigit()), parts)) or None
    return None


def uninstall_entry(entry: MCPEntry, root: Path) -> None:
    """Деинсталл MCP из venv. pip-типы → pip uninstall <dist>; npx → npm uninstall -g.
    Сносит ТОЛЬКО сам пакет (зависимости не трогает). Best-effort: ошибки → warn."""
    itype = entry.install.type
    if itype in ("pypi", "source", "wheel"):
        dist = _dist_name(entry, root)
        if not dist:
            print(f"[warn] {entry.name}: не удалось определить имя пакета — деинсталл пропущен")
            return
        print(f"[uninstall] {entry.name}: pip uninstall {dist}")
        subprocess.run(pip_argv("uninstall", "-y", dist), check=False)
    elif itype == "npx":
        pkg = entry.install.npm_package
        if not pkg:
            print(f"[warn] {entry.name}: npm_package не задан — деинсталл пропущен")
            return
        from config_modules.env_vars import gc_paths
        agent_dep_dir = gc_paths()["data"]
        npm = _node_install_paths(agent_dep_dir)["npm"]
        print(f"[uninstall] {entry.name}: npm uninstall -g {pkg}")
        subprocess.run([str(npm), "uninstall", "-g", pkg], check=False)
    else:
        print(f"[warn] {entry.name}: деинсталл для type={itype!r} не поддержан (вне scope)")


def install_entry(entry: MCPEntry, root: Path, agent_dep_dir: Optional[Path] = None) -> None:
    """Установить один MCP. root — каталог, относительно которого резолвится entry.path."""
    if not _platform_supported(entry):
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
        _install_npx(entry.name, entry.install.commands, agent_dep_dir)

    elif itype == "source":
        _install_source(entry.name, entry.install.source, root, editable=False)
    elif itype == "pypi":
        _install_pypi(entry.name, entry.install.package)
    else:
        raise InstallError(
            f"mcp {entry.name!r} uses install.type={itype!r} which is not supported "
            f"by this install.py. Update install.py to handle {itype!r}, "
            f"or comment out the block in config.yml."
        )


def _pypi_distribution_name(package_spec: str) -> str:
    match = re.match(r"^([^[<>=!;\s]+)", package_spec.strip())
    if not match:
        raise InstallError(f"invalid PyPI package spec: {package_spec!r}")
    return match.group(1).strip()


def _pypi_pinned_version(package_spec: str) -> str | None:
    if "==" not in package_spec:
        return None
    return package_spec.split("==", 1)[1].strip()


def _install_pypi(name: str, package_spec: str | None) -> None:
    if not package_spec:
        raise InstallError(f"mcp {name!r}: install.type=pypi requires install.package")
    dist_name = _pypi_distribution_name(package_spec)
    expected = _pypi_pinned_version(package_spec)
    current = query_installed_version(dist_name)
    if expected is not None and current == expected:
        print(f"[skip] {name}: {dist_name}=={expected} already installed")
        return
    print(f"[install] {name}: pip install {package_spec}")
    subprocess.run(pip_install_argv(package_spec), check=True)


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
    subprocess.run(pip_install_argv(str(wheel)), check=True)


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
        raise InstallError(f"mcp {name!r}: install.type=source requires install.source")
    src_path = (root / source).resolve()
    if not (src_path / "pyproject.toml").exists():
        raise InstallError(
            f"mcp {name!r}: pyproject.toml not found in {src_path} "
            f"(install.source must point to a Python package root)"
        )
    cmd = [str(active_pip_python()), "-m", "pip", "install"]
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


def _node_archive_basename() -> Tuple[str, str]:
    """Имя архива и его расширение для текущей платформы.

    Возвращает (filename, extension), где extension ∈ {tar.gz, tar.xz, zip}.
    Соответствует layout'у https://nodejs.org/dist/vX.Y.Z/.
    """
    machine = (
        os.uname().machine
        if hasattr(os, "uname")
        else os.environ.get("PROCESSOR_ARCHITECTURE", "")
    ).lower()
    if sys.platform == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
        return f"node-v{NODE_VERSION}-darwin-{arch}.tar.gz", "tar.gz"
    if sys.platform == "win32":
        arch = "arm64" if "arm64" in machine else "x64"
        return f"node-v{NODE_VERSION}-win-{arch}.zip", "zip"
    # Linux и прочие POSIX
    if machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine in ("armv7l", "armv6l"):
        arch = "armv7l"
    else:
        arch = "x64"
    return f"node-v{NODE_VERSION}-linux-{arch}.tar.xz", "tar.xz"


def _node_install_paths(agent_dep_dir: Path) -> dict[str, Path]:
    """Абсолютные пути до бинарей внутри `<agent_dep_dir>/node/v<NODE_VERSION>/`.

    Layout одинаковый на POSIX и Windows: бинари всегда в `<root>/bin/`.
    На Windows это противоречит дефолтному zip-layout-у (бинари в корне) —
    `_ensure_cached_node` принудительно перемещает распакованную папку
    в `<root>/bin/`, чтобы `command:` в config.yml был кроссплатформенным.
    """
    root = agent_dep_dir / "node" / f"v{NODE_VERSION}"
    bin_dir = root / "bin"
    if sys.platform == "win32":
        node = bin_dir / "node.exe"
        npm = bin_dir / "npm.cmd"
        npx = bin_dir / "npx.cmd"
    else:
        node = bin_dir / "node"
        npm = bin_dir / "npm"
        npx = bin_dir / "npx"
    return {"root": root, "bin": bin_dir, "node": node, "npm": npm, "npx": npx}


def _verify_existing_node(paths: dict[str, Path]) -> bool:
    """True, если все три бинаря на месте и `node --version` совпадает с NODE_VERSION."""
    if not (paths["node"].exists() and paths["npm"].exists() and paths["npx"].exists()):
        return False
    try:
        result = subprocess.run(
            [str(paths["node"]), "--version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.stdout.strip() == f"v{NODE_VERSION}"


def _fetch_expected_sha256(archive_filename: str) -> str:
    """Скачать SHASUMS256.txt и вытащить hash для archive_filename."""
    url = f"{_NODE_DIST_BASE}/v{NODE_VERSION}/SHASUMS256.txt"
    print(f"[npx] fetching SHASUMS256.txt from {url}")
    with urllib.request.urlopen(url, timeout=60) as resp:
        body = resp.read().decode("utf-8")
    for line in body.splitlines():
        # Формат: "<64hex>  <filename>"
        parts = line.split()
        if len(parts) == 2 and parts[1] == archive_filename:
            return parts[0]
    raise InstallError(
        f"SHASUMS256.txt at {url} doesn't list {archive_filename!r}. "
        f"Either NODE_VERSION pin is wrong or nodejs.org layout changed."
    )


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_archive(archive_filename: str, dest: Path) -> None:
    url = f"{_NODE_DIST_BASE}/v{NODE_VERSION}/{archive_filename}"
    print(f"[npx] downloading {url}")
    urllib.request.urlretrieve(url, str(dest))


def _extract_archive(archive_path: Path, ext: str, dest_parent: Path) -> Path:
    """
    Распаковать архив Node в dest_parent. Возвращает путь до распакованной
    верхней папки (`node-vX.Y.Z-<platform>-<arch>/`).
    """
    if ext == "zip":
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(dest_parent)
            top = zf.namelist()[0].split("/", 1)[0]
    elif ext in ("tar.gz", "tar.xz"):
        mode = "r:gz" if ext == "tar.gz" else "r:xz"
        with tarfile.open(archive_path, mode=mode) as tf:
            tf.extractall(dest_parent)
            top = tf.getnames()[0].split("/", 1)[0]
    else:
        raise InstallError(f"unsupported archive extension {ext!r}")
    return dest_parent / top


def _ensure_cached_node(agent_dep_dir: Path) -> dict[str, Path]:
    """
    Гарантировать наличие пиннутого Node под `<agent_dep_dir>/node/v<NODE_VERSION>/`.

    Идемпотентно: если бинари на месте и версия совпадает — ничего не качаем.
    Системный Node игнорируется полностью — даже если в PATH есть Node 22,
    мы используем только нашу копию (воспроизводимость + защита от того, что
    апгрейд системного Node уронит MCP).
    """
    paths = _node_install_paths(agent_dep_dir)
    if _verify_existing_node(paths):
        print(f"[npx] cached Node v{NODE_VERSION} already at {paths['root']}")
        return paths

    archive_filename, ext = _node_archive_basename()
    expected_sha = _fetch_expected_sha256(archive_filename)

    # Качаем во временный файл, чтобы прерванная загрузка не оставила
    # битый архив рядом с целью.
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        archive_path = tmp / archive_filename
        _download_archive(archive_filename, archive_path)

        actual_sha = _sha256_of_file(archive_path)
        if actual_sha != expected_sha:
            raise InstallError(
                f"SHA256 mismatch for {archive_filename}: "
                f"expected {expected_sha}, got {actual_sha}"
            )
        print(f"[npx] sha256 verified")

        extracted_top = _extract_archive(archive_path, ext, tmp)

        # POSIX-таркомпрессия даёт уже layout `node-vX.Y.Z-<plat>/bin/...` —
        # двигаем extracted_top в `<root>/`. Windows-zip кладёт бинари в
        # корень `node-vX.Y.Z-win-x64/`, без `bin/`. Чтобы layout был
        # кроссплатформенный (`<root>/bin/npx[.cmd]` на обеих ОС), на
        # Windows двигаем содержимое архива в `<root>/bin/`.
        # `npm.cmd`/`npx.cmd` используют `%~dp0` для поиска `node_modules/` —
        # перемещение всей распакованной папки целиком сохраняет относительные
        # ссылки внутри.
        if sys.platform == "win32":
            target = paths["bin"]
        else:
            target = paths["root"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if paths["root"].exists():
            shutil.rmtree(paths["root"])
        try:
            shutil.move(str(extracted_top), str(target))
        except OSError:
            shutil.copytree(str(extracted_top), str(target))
            shutil.rmtree(str(extracted_top), ignore_errors=True)

    if not _verify_existing_node(paths):
        raise InstallError(
            f"Node install completed but {paths['node']} doesn't report v{NODE_VERSION}. "
            f"Inspect {paths['root']} and re-run install.py."
        )
    print(f"[npx] installed Node v{NODE_VERSION} at {paths['root']}")
    return paths


def _install_npx(
    name: str,
    commands: tuple[tuple[str, ...], ...],
    agent_dep_dir: Path,
) -> None:
    """Установка MCP, доставляемого через npx, на нашей пиннутой Node.

    Шаги:
      1. ensure cached Node (идемпотентно).
      2. prepend `<cache>/node/.../bin` в PATH текущего процесса — чтобы
         внутренние вызовы npm-из-npx, node-из-npm и т.д. брали нашу версию,
         а не системную.
      3. прогнать install.commands из config.yml. argv[0] in {npx, npm, node}
         подменяется на абсолютный путь из cached Node — на Windows .cmd-shim
         напрямую запускается без shell=True, на POSIX просто абс. путь.
    """
    paths = _ensure_cached_node(agent_dep_dir)
    bin_dir = str(paths["bin"])
    if bin_dir not in os.environ.get("PATH", ""):
        sep = ";" if sys.platform == "win32" else ":"
        os.environ["PATH"] = bin_dir + sep + os.environ.get("PATH", "")

    binmap: dict[str, str] = {
        "npx": str(paths["npx"]),
        "npm": str(paths["npm"]),
        "node": str(paths["node"]),
    }

    for cmd in commands:
        argv = list(cmd)
        if argv and argv[0] in binmap:
            argv[0] = binmap[argv[0]]
        print(f"[install] {name}: {' '.join(argv)}")
        subprocess.run(argv, check=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Install MCPs from recommended_mcp/config.yml",
    )
    
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        metavar="PATH",
        help="Путь к кастомному конфигурационному файлу манифеста MCP "
        "(по умолчанию recommended_mcp/config.yml).",
    )
    parser.add_argument(
        "-e",
        "--editable",
        action="store_true",
        help="Install source-type MCPs in editable mode (pip install -e). "
        "Применяется только к install.type=source. "
        "Для wheel/pypi/docker/npx игнорируется.",
    )
    args = parser.parse_args()

    mcp_py = ensure_mcp_venv()
    print(f"[python] recommended_mcp/venv → {mcp_py}")

    manifest_path = resolve_install_manifest_path(args.config)
    root = recipe_root()
    print(f"[config] {manifest_path}")
    agent_dep_dir = load_agent_dep_dir(manifest_path)
    manifest = load_manifest(manifest_path)
    if not manifest:
        print("[info] no mcps declared in config.yml — nothing to install")
        return 0
    with pip_install_target(mcp_py):
        results = run_install_entries(manifest, root, agent_dep_dir, editable=args.editable)
    installed = [n for n, s, _ in results if s == "installed"]
    skipped = [(n, d) for n, s, d in results if s == "skipped"]
    failed = [(n, d) for n, s, d in results if s == "failed"]
    print(f"[summary] установлено: {', '.join(installed) or '—'}")
    for n, d in skipped:
        print(f"[summary] пропущено ({d}): {n}")
    for n, d in failed:
        print(f"[summary] НЕ установлено: {n} — {d}")
    if failed:
        print(f"[summary] {len(failed)} MCP пропущено — агент будет работать; "
              f"поставь вручную или используй совместимый Python")
    state = save_runtime_state(manifest_path=manifest_path)
    print(f"[state] {state.bin_dir}")
    paths = _node_install_paths(agent_dep_dir)
    if paths["npx"].exists():
        from config_modules.env_vars import gc_paths

        cache = gc_paths()["cache"]
        print(f"[paths] config command: {paths['npx']}")
        print(f"[paths] playwright --output-dir: {cache / 'playwright-mcp'}")
        print(f"[paths] MCP media (agent default): {cache / 'mcp-media'}")
    print("[done] install complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
