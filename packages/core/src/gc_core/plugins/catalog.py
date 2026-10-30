"""Сборка снимка набора плагинов, его отпечаток и доверие (§3.2, §7.1, §8.3).

Набор — ``plugins.paths``, git-источники ``plugins.sources`` и включённые
установленные плагины. Режимы, в которых плагины в первой версии не
поддерживаются (единый источник skills, нефайловое хранилище агента,
удалённый OS-бэкенд), дают пустой снимок с причиной в ``notices``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from gc_core.backends.factory import parse_os_backend_kind
from gc_core.config_modules.plugins import PluginsConfig
from gc_core.config_modules.storage import FILES
from gc_core.plugins import installed, sources
from gc_core.plugins.manifest import (
    PLUGIN_FORMAT_AP,
    Plugin,
    PluginCommand,
    load_plugin,
    skill_id_segment,
)
from gc_core.plugins.mcp_servers import plugin_mcp_part
from gc_core.plugins.snapshot import PluginSnapshot, SkillRoot
from gc_core.settings import LoadedSettings

logger = logging.getLogger(__name__)

_Stamp = tuple[int, int]
_Loaded = tuple[tuple[str, Plugin], ...]
_cache: dict[tuple[tuple[str, _Stamp], ...], _Loaded] = {}


@dataclass(frozen=True)
class PluginEntries:
    """Каталоги набора по порядку и какие из них — установленные."""

    paths: tuple[str, ...]
    installed: frozenset[str]
    notices: tuple[str, ...]


def _installed_paths(cfg: PluginsConfig) -> list[str]:
    if not cfg.use_installed:
        return []
    try:
        return installed.enabled_paths()
    except Exception:  # хранилище не должно ронять агента
        logger.warning("installed plugins unavailable", exc_info=True)
        return []


def _source_notice(source) -> str:
    head = f"Плагин из источника {source.git} не подключён: "
    if sources.is_cloned(source) and source.subdir:
        return head + f"в репозитории нет подкаталога {source.subdir}."
    return head + "репозиторий не получен."


def plugin_entries(cfg: PluginsConfig, *, clone: bool) -> PluginEntries:
    """Каталоги плагинов набора. Git запускается только при ``clone=True``;
    источник без клона (или с неудачным клоном) — в ``notices``."""
    paths = list(cfg.paths)
    notices: list[str] = []
    for source in cfg.sources:
        path = sources.resolve_source(source, fetch=clone)
        if path is None:
            notices.append(_source_notice(source))
        else:
            paths.append(str(path))
    found = _installed_paths(cfg)
    paths += [p for p in found if p not in paths]
    return PluginEntries(tuple(paths), frozenset(found), tuple(notices))


def _dir_stamp(path: str) -> _Stamp:
    """Отпечаток каталога: ``update`` подменяет каталог целиком (новый
    inode; время изменения ``copytree`` копирует из источника, его мало)."""
    try:
        st = os.stat(path)
    except OSError:
        return (-1, -1)
    return (st.st_ino, st.st_mtime_ns)


def _load_entries(paths: tuple[str, ...]) -> _Loaded:
    """``(каталог, плагин)``; одноимённый плагин — первый из списка.

    В ключе кеша и отпечатки каталогов: долгоживущий процесс после
    ``gc-plugin update`` читает плагин заново."""
    key = tuple((p, _dir_stamp(p)) for p in paths)
    if key not in _cache:
        unique: dict[str, tuple[str, Plugin]] = {}
        for path in paths:
            plugin = load_plugin(path)
            if plugin is not None and plugin.name not in unique:
                unique[plugin.name] = (path, plugin)
        _cache[key] = tuple(unique.values())
    return _cache[key]


def load_plugins(paths: list[str]) -> tuple[Plugin, ...]:
    return tuple(plugin for _, plugin in _load_entries(tuple(paths)))


def reset_plugin_cache() -> None:
    """Сбросить кеш прочитанных плагинов (для тестов и перезагрузки)."""
    _cache.clear()
    sources._warned.clear()


def skill_roots(plugins: tuple[Plugin, ...]) -> tuple[SkillRoot, ...]:
    """Корни skills; корень вложенности — только у Agent Plugins (их skills
    за пределами плагина, по ссылке, пропускаются)."""
    return tuple(
        SkillRoot(
            p.name,
            str(d),
            str(p.root) if p.format == PLUGIN_FORMAT_AP else None,
        )
        for p in plugins
        for d in p.skills_dirs
    )


def _skill_ids(roots: tuple[SkillRoot, ...]) -> frozenset[str]:
    """Id skills плагинов (``<плагин>:<имя>``), как их заведёт реестр."""
    ids: set[str] = set()
    for root in roots:
        for entry in Path(root.path).iterdir():
            if not (entry / "SKILL.md").is_file():
                continue
            if root.contain and not entry.resolve().is_relative_to(
                Path(root.contain).resolve()
            ):
                continue
            ids.add(f"{root.plugin}:{skill_id_segment(entry)}")
    return frozenset(ids)


def _plugin_commands(
    plugins: tuple[Plugin, ...], roots: tuple[SkillRoot, ...]
) -> tuple[tuple[tuple[str, PluginCommand], ...], tuple[str, ...]]:
    """Команды ``(плагин, команда)`` и диагностика: команда с id skill
    отбрасывается — skill главнее (§4.2)."""
    skill_ids = _skill_ids(roots)
    kept: list[tuple[str, PluginCommand]] = []
    notices: list[str] = []
    for plugin in plugins:
        for command in plugin.commands:
            full_id = f"{plugin.name}:{command.name}"
            if full_id in skill_ids:
                notices.append(
                    f"Команда плагина {full_id} не подключена: есть skill "
                    "с тем же именем."
                )
            else:
                kept.append((plugin.name, command))
    return tuple(kept), tuple(notices)


def _disabled_reason(loaded: LoadedSettings) -> str | None:
    settings = loaded.settings
    if settings.paths.skills_data_path:
        return (
            "Плагины выключены: задан paths.skills_data_path "
            "(единый источник skills)."
        )
    if settings.storage.part("agent")[0] != FILES:
        return "Плагины выключены: storage.agent — не файловое хранилище."
    # Пакет тулов тяжёлый (~1.4 с): только когда до проверки бэкенда дошло.
    from gc_core.tools import _REMOTE_OS_BACKENDS

    kind = parse_os_backend_kind(loaded)
    if kind in _REMOTE_OS_BACKENDS:
        return f"Плагины выключены: OS-бэкенд {kind} не локальный."
    return None


def _has_plugins(cfg: PluginsConfig) -> bool:
    return bool(cfg.paths or cfg.sources or _installed_paths(cfg))


def plugin_fingerprint(loaded: LoadedSettings) -> tuple:
    """Дешёвый отпечаток набора без чтения плагинов: режим, недоверенные
    записи, ``.installed.json`` и отпечатки каталогов."""
    cfg = loaded.settings.plugins
    paths = [
        *cfg.paths,
        *(str(sources.source_dir(s)) for s in cfg.sources),
        *_installed_paths(cfg),
    ]
    return (
        _disabled_reason(loaded),
        tuple(sorted(loaded.runtime.untrusted_plugin_entries)),
        _dir_stamp(str(installed.metadata_path())),
        tuple((p, _dir_stamp(p)) for p in paths),
    )


def build_plugin_snapshot(
    loaded: LoadedSettings, *, clone: bool
) -> PluginSnapshot:
    """Снимок набора по настройкам после всех оверрайдов."""
    cfg = loaded.settings.plugins
    fingerprint = plugin_fingerprint(loaded)
    reason = _disabled_reason(loaded)
    if reason is not None:
        notices = (reason,) if _has_plugins(cfg) else ()
        return PluginSnapshot(notices=notices, fingerprint=fingerprint)
    entries = plugin_entries(cfg, clone=clone)
    loaded_entries = _load_entries(entries.paths)
    untrusted = loaded.runtime.untrusted_plugin_entries
    plugins = tuple(plugin for _, plugin in loaded_entries)
    servers, keys, mcp_notices = plugin_mcp_part(
        plugins, loaded.settings.mcp.servers
    )
    roots = skill_roots(plugins)
    commands, command_notices = _plugin_commands(plugins, roots)
    return PluginSnapshot(
        plugins=plugins,
        trusted=frozenset(
            plugin.name
            for path, plugin in loaded_entries
            if path in entries.installed or path not in untrusted
        ),
        skill_roots=roots,
        commands=commands,
        mcp_servers=servers,
        mcp_keys=keys,
        notices=entries.notices + mcp_notices + command_notices,
        fingerprint=fingerprint,
    )


def with_plugin_snapshot(
    loaded: LoadedSettings, *, clone: bool = True
) -> LoadedSettings:
    """Настройки со снимком набора в рантайме; его диагностика — в notices."""
    snap = build_plugin_snapshot(loaded, clone=clone)
    return loaded.with_runtime(
        plugins=snap, notices=loaded.runtime.notices + snap.notices
    )
