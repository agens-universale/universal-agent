"""Данные карточек экрана ``/plugins`` по снимку набора (§12).

Состав плагина, его диагностика из снимка (свои строки у субагентов), а
у подключённых субагентов — доступность при текущих настройках и права. Читает
диск и обходит определения субагентов — звать вне цикла интерфейса.
Своих решений о плагинах не принимает: причины дают снимок и функции ядра.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from gc_core.plugins import installed as _store
from gc_core.plugins.catalog import plugin_entries, plugin_skill_ids
from gc_core.plugins.installed import list_installed
from gc_core.plugins.manifest import (
    Plugin,
    PluginAgent,
    load_plugin,
    shown_matcher,
)
from gc_core.plugins.sources import shown_url
from gc_core.settings import runtime_of
from gc_core.tools.subagents.role_info import role_availability, role_rights

_LOAD_FAILED = "не загружен (см. лог)"
# Диагностика снимка по плагинам: субагент, команда, хуки, MCP-сервер.
_OWNER = re.compile(
    r"^(?:Субагент (?P<role>[^\s:]+):"
    r"|Команда плагина (?P<command>[^\s:]+):"
    r"|Хуки плагина (?P<hooks>\S+) "
    r"|MCP-сервер .+? плагина (?P<mcp>\S+) )"
)
# Запись пользователя заменила сервер плагина — сведение, не неполадка.
_INFO_MARK = " заменён записью "


def notice_owner(notice: str) -> str | None:
    """Плагин, к которому относится строка диагностики снимка."""
    match = _OWNER.match(notice)
    if match is None:
        return None
    return next(g for g in match.groups() if g is not None)


def general_notices(settings: Any) -> list[str]:
    """Диагностика снимка не по одному плагину: режим без плагинов,
    неподключённый git-источник."""
    return [
        n
        for n in runtime_of(settings).plugins.notices
        if notice_owner(n) is None
    ]


def _hook_label(event: str, matcher: str) -> str:
    shown = shown_matcher(event, matcher)
    return event if shown is None else f"{event} {shown}"


def _is_installed_path(root: Path) -> bool:
    try:
        root.resolve().relative_to(_store.installed_dir().resolve())
    except ValueError:
        return False
    return True


def _source(item) -> str:
    source = shown_url(item.source)
    return f"{source}@{item.commit[:8]}" if item.commit else source


def _version(version: str) -> str:
    return "—" if version in ("", "0") else version


def _bare_card(name, version, source, *, enabled, error="") -> dict:
    """Карточка без состава: выключенный или не загрузившийся плагин."""
    return {
        "name": name,
        "version": _version(version),
        "format": "",
        "source": source,
        "enabled": enabled,
        "skills": [],
        "commands": [],
        "hooks": [],
        "mcp_servers": [],
        "roles": [],
        "warnings": [],
        "info": [],
        "error": error,
    }


def _rights_text(settings: Any, role: Any) -> str:
    rights = role_rights(settings, role)
    if rights.all_tools:
        parts = ["все инструменты"]
    else:
        parts = [
            label
            for label, on in (
                ("правка файлов", rights.edit),
                ("команды", rights.exec),
            )
            if on
        ]
    if rights.mcp:
        parts.append("MCP")
    return ", ".join(parts) or "только чтение"


def _role_entry(
    settings: Any, agent: PluginAgent, role: Any, notes: list[str]
) -> dict:
    """Строка субагента: права и доступность у подключённого; у неподключённого
    (битая шапка, ничего не переведено) — причина из диагностики."""
    if role is None:
        fallback = (
            f"не подключён: {agent.problem}"
            if agent.problem
            else "не подключён"
        )
        head = next(
            (n for n in notes if n.startswith("не подключён")), fallback
        )
        rest = [n for n in notes if n != head]
        return {"name": agent.name, "status": head, "ok": False, "notes": rest}
    status = _rights_text(settings, role)
    reason = role_availability(settings, role.name)
    if reason:
        status += f" · недоступен: {reason}"
    return {
        "name": agent.name,
        "status": status,
        "ok": reason is None,
        "notes": notes,
    }


def _role_notes(plugin: str, agent: str, notices: list[str]) -> list[str]:
    """Диагностика субагента без префикса «Субагент <плагин>:<имя>»."""
    label = f"Субагент {plugin}:{agent}"
    return [
        n[len(label) :].lstrip(": ")
        for n in notices
        if n.startswith((label + " ", label + ":"))
    ]


def _plugin_card(settings: Any, plugin: Plugin, item: Any) -> dict:
    snapshot = runtime_of(settings).plugins
    own = [n for n in snapshot.notices if notice_owner(n) == plugin.name]
    by_source = {r.source_path: r for r in snapshot.roles}
    roles = [
        _role_entry(
            settings,
            agent,
            by_source.get(agent.source),
            _role_notes(plugin.name, agent.name, own),
        )
        for agent in plugin.agents
    ]
    rest = [n for n in own if not n.startswith(f"Субагент {plugin.name}:")]
    from_store = item is not None and _is_installed_path(plugin.root)
    return {
        "name": plugin.name,
        "version": _version(plugin.version),
        "format": plugin.format,
        "source": _source(item) if from_store else "config",
        "enabled": True,
        "skills": plugin_skill_ids(plugin),
        "commands": [c.name for c in plugin.commands],
        "hooks": [
            _hook_label(event, group.matcher)
            for event, groups in plugin.hooks.items()
            for group in groups
        ],
        "mcp_servers": list(plugin.mcp_servers),
        "roles": roles,
        "warnings": [
            *plugin.hook_problems,
            *plugin.mcp_problems,
            *(n for n in rest if _INFO_MARK not in n),
        ],
        "info": [n for n in rest if _INFO_MARK in n],
        "error": "",
    }


def _failed_cards(paths, plugins, installed) -> list[dict]:
    """Каталоги набора, которые ``load_plugin`` не прочитал."""
    loaded = {p.root for p in plugins}
    cards: list[dict] = []
    for path in paths:
        root = Path(path).expanduser().resolve()
        if root in loaded or load_plugin(root) is not None:
            continue
        item = installed.get(root.name) if _is_installed_path(root) else None
        cards.append(
            _bare_card(
                root.name,
                item.version if item else "",
                _source(item) if item else "config",
                enabled=True,
                error=_LOAD_FAILED,
            )
        )
    return cards


def plugin_cards(settings: Any) -> list[dict]:
    """Карточки: плагины снимка, не загрузившиеся и выключенные."""
    installed = {p.name: p for p in list_installed()}
    plugins = runtime_of(settings).plugins.plugins
    cards = [
        _plugin_card(settings, plugin, installed.get(plugin.name))
        for plugin in plugins
    ]
    paths = plugin_entries(settings.settings.plugins, clone=False).paths
    cards += _failed_cards(paths, plugins, installed)
    shown = {c["name"] for c in cards}
    cards += [
        _bare_card(item.name, item.version, _source(item), enabled=False)
        for item in installed.values()
        if item.name not in shown and not item.enabled
    ]
    return cards
