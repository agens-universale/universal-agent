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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from gc_core.plugins.manifest import Plugin, ap_manifest_errors, load_plugin
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


def plugin_data_path(name: str) -> Path:
    from gc_core.config_modules import env_vars

    return (
        Path(env_vars.gc_paths()["data"])
        / "plugins"
        / "data"
        / _safe_name(name)
    )


def plugin_data_dir(name: str) -> Path:
    """Постоянный каталог данных плагина (``${PLUGIN_DATA}``); создаётся здесь.

    ``OSError`` (например, только для чтения) обрабатывает вызывающий.
    """
    path = plugin_data_path(name)
    path.mkdir(parents=True, exist_ok=True)
    return path


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


class PreparedInstall:
    """Скачанный и проверенный плагин, ещё не скопированный в хранилище.

    Нужен, чтобы показать пользователю, что плагин запускает, до установки.
    Временный каталог живёт, пока открыт контекст ``prepare``.
    """

    def __init__(
        self,
        *,
        plugin: Plugin,
        plugin_dir: Path,
        source: str,
        ref: str | None,
        subdir: str | None,
        sha: str | None,
        root: Path,
    ) -> None:
        self.plugin = plugin
        self._plugin_dir = plugin_dir
        self._source = source
        self._ref = ref
        self._subdir = subdir
        self._sha = sha
        self._root = root

    def summary(self) -> list[str]:
        """Строки для подтверждения: записи как в файлах плагина.

        ``${VAR}`` и ``${CLAUDE_PLUGIN_ROOT}`` / ``${PLUGIN_ROOT}`` показаны
        буквально (раскрытые значения могут быть секретами и уйти в лог CI);
        значения ``env`` и ``headers`` не попадают. Пропущенные серверы MCP
        идут отдельными строками с причиной.
        """
        plugin = self.plugin
        lines = [
            f"{event} [{m.matcher or '*'}]: {h.command}"
            for event, matchers in plugin.hooks.items()
            for m in matchers
            for h in m.hooks
        ]
        for name, srv in plugin.mcp_servers_raw.items():
            if srv.get("url"):
                lines.append(f"mcp {name}: {srv['url']}")
            else:
                cmd = " ".join(
                    [
                        str(srv.get("command", "")),
                        *map(str, srv.get("args", [])),
                    ]
                )
                lines.append(f"mcp {name}: {cmd}")
        lines.extend(plugin.mcp_problems)
        skills = 0
        if plugin.skills_dir:
            skills = sum(
                1
                for d in Path(plugin.skills_dir).iterdir()
                if (d / "SKILL.md").is_file()
            )
        lines.append(
            f"skills: {skills}, commands: {len(plugin.commands)}, "
            f"agents: {len(plugin.agents)}"
        )
        return lines

    def differs_from(self, name: str) -> bool:
        """Отличаются ли хуки или MCP от установленной копии ``name``."""
        old = load_plugin(self._root / _safe_name(name))
        return old is None or _exec_surface(old) != _exec_surface(self.plugin)

    def ensure_free(self, force: bool = False) -> None:
        """``ValueError``, если плагин с этим именем уже установлен (без ``force``)."""
        name = _safe_name(self.plugin.name)
        if (self._root / name).exists() and not force:
            raise ValueError(
                f"плагин {name} уже установлен (--force, чтобы заменить)"
            )

    def commit(self, force: bool = False) -> InstalledPlugin:
        """Скопировать в хранилище и записать метаданные."""
        plugin = self.plugin
        self.ensure_free(force)
        name = _safe_name(plugin.name)
        dest = self._root / name
        meta = _read_meta(self._root)
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(
            self._plugin_dir, dest, ignore=shutil.ignore_patterns(".git")
        )
        info = InstalledPlugin(
            name=name,
            version=plugin.version,
            source=self._source,
            ref=self._ref,
            subdir=self._subdir,
            commit=self._sha,
            enabled=True,
        )
        meta[name] = asdict(info)
        _write_meta(self._root, meta)
        return info


def _exec_surface(plugin: Plugin) -> set[tuple]:
    """Всё, что плагин запускает: хуки и MCP как в файлах (без подстановок,
    env и headers), чтобы путь копии и окружение не считались изменением."""
    hooks = {
        ("hook", event, m.matcher, h.command)
        for event, matchers in plugin.hooks.items()
        for m in matchers
        for h in m.hooks
    }
    mcp = {
        (
            "mcp",
            name,
            srv.get("transport"),
            srv.get("command"),
            tuple(srv.get("args") or ()),
            srv.get("url"),
        )
        for name, srv in plugin.mcp_servers_raw.items()
    }
    return hooks | mcp


@contextmanager
def prepare(
    source: str,
    *,
    ref: str | None = None,
    subdir: str | None = None,
    root: Path | None = None,
) -> Iterator[PreparedInstall]:
    """Скачать и проверить плагин; ``ValueError``, если источник не плагин."""
    root = root or installed_dir()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as tmp_name:
        fetched, sha, norm = _fetch(source, ref, Path(tmp_name))
        plugin_dir = fetched / subdir if subdir else fetched
        errors = ap_manifest_errors(plugin_dir)
        if errors:
            raise ValueError(f"{source}: " + "; ".join(errors))
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
            if plugin.mcp_problems:
                raise ValueError(
                    f"{source}: MCP не загружен: "
                    + "; ".join(plugin.mcp_problems)
                )
            raise ValueError(
                f"{source}: нет ни skills/, commands/, agents/, "
                "ни hooks/hooks.json, ни .mcp.json/mcp.json"
            )
        yield PreparedInstall(
            plugin=plugin,
            plugin_dir=plugin_dir,
            source=norm,
            ref=ref,
            subdir=subdir,
            sha=sha,
            root=root,
        )


def install(
    source: str,
    *,
    ref: str | None = None,
    subdir: str | None = None,
    force: bool = False,
    root: Path | None = None,
) -> InstalledPlugin:
    """Установить плагин без вопросов; ``ValueError``, если источник не плагин или имя занято."""
    with prepare(source, ref=ref, subdir=subdir, root=root) as prepared:
        return prepared.commit(force)


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


def update(
    name: str,
    root: Path | None = None,
    confirm: Callable[[PreparedInstall], bool] | None = None,
) -> InstalledPlugin | None:
    """Переустановить из исходного источника с тем же ref (ветка уйдёт вперёд).

    ``confirm`` вызывается, только если хуки или MCP отличаются от установленной
    копии; отказ даёт ``ValueError``.
    """
    root, meta = _load_meta(root)
    rec = meta.get(name)
    if rec is None:
        return None
    was_enabled = bool(rec.get("enabled", True))
    with prepare(
        rec["source"], ref=rec.get("ref"), subdir=rec.get("subdir"), root=root
    ) as prepared:
        if confirm and prepared.differs_from(name) and not confirm(prepared):
            raise ValueError("обновление отменено")
        info = prepared.commit(force=True)
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
