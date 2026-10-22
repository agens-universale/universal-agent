"""Установленные плагины: постоянное хранилище.

Плагин ставится командой ``gc-plugin install`` в ``<gc_data>/plugins/installed/
<имя>/`` (копия без ``.git``) и сразу работает без правки конфига. Метаданные
лежат в ``.installed.json`` рядом: источник, запрошенный ref, коммит,
включён ли плагин. ``disable`` оставляет файлы, но плагин не загружается;
``uninstall`` удаляет каталог. Источник: путь, URL или ``owner/repo`` (GitHub).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from gc_core.plugins.manifest import load_plugin
from gc_core.plugins.sources import _clone, normalize_url

logger = logging.getLogger(__name__)

_META = ".installed.json"


@dataclass
class InstalledPlugin:
    name: str
    version: str
    source: str
    ref: str | None = None
    subdir: str | None = None
    commit: str | None = None
    enabled: bool = True


def installed_dir() -> Path:
    from gc_core.config_modules import env_vars

    return Path(env_vars.gc_paths()["data"]) / "plugins" / "installed"


def _meta_path(root: Path) -> Path:
    return root / _META


def _read_meta(root: Path) -> dict[str, dict]:
    try:
        data = json.loads(_meta_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(root: Path, meta: dict[str, dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _meta_path(root).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _safe_name(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", name).strip("._") or "plugin"


def _commit_of(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _fetch(
    source: str, ref: str | None, tmp: Path
) -> tuple[Path, str | None, str]:
    """Положить источник в ``tmp``: (каталог, коммит, нормализованный источник)."""
    local = Path(source).expanduser()
    if local.exists():
        target = tmp / "src"
        shutil.copytree(
            local.resolve(), target, ignore=shutil.ignore_patterns(".git")
        )
        return target, _commit_of(local.resolve()), str(local.resolve())
    url = normalize_url(source)
    target = tmp / "clone"
    _clone(url, ref, target)
    return target, _commit_of(target), url


def install(
    source: str,
    *,
    ref: str | None = None,
    subdir: str | None = None,
    force: bool = False,
    root: Path | None = None,
) -> InstalledPlugin:
    """Установить плагин; ``ValueError``, если источник не плагин или имя занято."""
    root = root or installed_dir()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as tmp_name:
        tmp = Path(tmp_name)
        fetched, commit, norm = _fetch(source, ref, tmp)
        plugin_dir = fetched / subdir if subdir else fetched
        plugin = load_plugin(plugin_dir)
        if plugin is None:
            raise ValueError(f"{source}: каталог плагина не найден")
        if not (
            plugin.skills_dir
            or plugin.hooks
            or plugin.mcp_servers
            or plugin.commands
            or plugin.agents
        ):
            raise ValueError(
                f"{source}: нет ни skills/, commands/, agents/, "
                "ни hooks/hooks.json, ни .mcp.json/mcp.json"
            )
        name = _safe_name(plugin.name)
        dest = root / name
        meta = _read_meta(root)
        if dest.exists() and not force:
            raise ValueError(
                f"плагин {name} уже установлен (--force, чтобы заменить)"
            )
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(
            plugin_dir, dest, ignore=shutil.ignore_patterns(".git")
        )
        info = InstalledPlugin(
            name=name,
            version=plugin.version,
            source=norm,
            ref=ref,
            subdir=subdir,
            commit=commit,
            enabled=True,
        )
        meta[name] = asdict(info)
        _write_meta(root, meta)
        return info


def resolve_name(name: str, root: Path | None = None) -> str:
    """Имя плагина по имени или по источнику (``obra/superpowers``, URL, путь)."""
    root, meta = _load_meta(root)
    if name in meta:
        return name
    wanted = normalize_url(name).removesuffix(".git").rstrip("/")
    for key, rec in meta.items():
        if (
            str(rec.get("source", "")).removesuffix(".git").rstrip("/")
            == wanted
        ):
            return key
    tail = wanted.rsplit("/", 1)[-1]
    return tail if tail in meta else name


def _load_meta(root: Path | None) -> tuple[Path, dict[str, dict]]:
    root = root or installed_dir()
    return root, _read_meta(root)


def list_installed(root: Path | None = None) -> list[InstalledPlugin]:
    """Установленные плагины; запись без каталога на диске отбрасывается."""
    root, meta = _load_meta(root)
    out: list[InstalledPlugin] = []
    for name, rec in sorted(meta.items()):
        if (root / name).is_dir():
            try:
                out.append(InstalledPlugin(**rec))
            except TypeError:
                logger.warning("installed plugin %s: bad metadata", name)
    return out


def _set_enabled(name: str, enabled: bool, root: Path | None) -> bool:
    root, meta = _load_meta(root)
    if name not in meta or not (root / name).is_dir():
        return False
    meta[name]["enabled"] = enabled
    _write_meta(root, meta)
    return True


def enable(name: str, root: Path | None = None) -> bool:
    return _set_enabled(name, True, root)


def disable(name: str, root: Path | None = None) -> bool:
    return _set_enabled(name, False, root)


def uninstall(name: str, root: Path | None = None) -> bool:
    root, meta = _load_meta(root)
    existed = name in meta or (root / name).is_dir()
    meta.pop(name, None)
    shutil.rmtree(root / name, ignore_errors=True)
    if existed:
        _write_meta(root, meta)
    return existed


def update(name: str, root: Path | None = None) -> InstalledPlugin | None:
    """Переустановить из исходного источника с тем же ref (ветка уйдёт вперёд)."""
    root, meta = _load_meta(root)
    rec = meta.get(name)
    if rec is None:
        return None
    was_enabled = bool(rec.get("enabled", True))
    info = install(
        rec["source"],
        ref=rec.get("ref"),
        subdir=rec.get("subdir"),
        force=True,
        root=root,
    )
    if not was_enabled:
        disable(info.name, root)
        info.enabled = False
    return info


def enabled_paths(root: Path | None = None) -> list[str]:
    """Каталоги включённых установленных плагинов (для загрузчика)."""
    root = root or installed_dir()
    if not root.is_dir():
        return []
    return [str(root / p.name) for p in list_installed(root) if p.enabled]
