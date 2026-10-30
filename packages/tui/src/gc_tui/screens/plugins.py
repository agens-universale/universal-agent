"""PluginsScreen — установленные плагины (read-only). Карточки как в /mcp."""

from __future__ import annotations

from pathlib import Path

from gc_core.plugins import installed as _store
from gc_core.plugins.installed import list_installed
from gc_core.plugins.manifest import load_plugin
from gc_core.plugins.runtime import configured_paths, load_plugins
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from gc_tui.palette import DEEP_DIM as _DEEP_DIM
from gc_tui.palette import DIM as _DIM
from gc_tui.palette import ERR as _ERR
from gc_tui.palette import OK as _OK
from gc_tui.screens.mcp import ServerCard

_FOOTER = "[Enter] раскрыть  [↑↓] плагины  [Esc] back"
_HINT = "Управление: gc-plugin install | enable | disable | uninstall"
_EMPTY = "Плагинов нет. Установка: gc-plugin install <owner/repo>"
_AGENTS_NOTE = "агенты плагина выключены: agent.use_local_subagents"
_LOAD_FAILED = "не загружен (см. лог)"


def _skill_ids(plugin) -> list[str]:
    return [
        f"{plugin.name}:{d.name}"
        for root in plugin.skills_dirs
        for d in sorted(root.iterdir())
        if (d / "SKILL.md").is_file()
    ]


def _is_installed_path(root) -> bool:
    try:
        root.resolve().relative_to(_store.installed_dir().resolve())
    except ValueError:
        return False
    return True


def _source(item) -> str:
    return f"{item.source}@{item.commit[:8]}" if item.commit else item.source


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
        "agents": [],
        "hooks": [],
        "mcp_servers": [],
        "agents_off": False,
        "error": error,
    }


def _failed_cards(paths, plugins, installed) -> list[dict]:
    """Включённые каталоги, которые ``load_plugin`` не прочитал."""
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


def plugin_cards(settings) -> list[dict]:
    """Данные карточек: загруженные, не загрузившиеся и выключенные плагины."""
    installed = {p.name: p for p in list_installed()}
    subagents = bool(settings.settings.agent.use_local_subagents)
    paths = configured_paths(settings.settings.plugins)
    plugins = load_plugins(paths)
    cards: list[dict] = []
    for plugin in plugins:
        item = installed.get(plugin.name)
        from_store = item is not None and _is_installed_path(plugin.root)
        cards.append(
            {
                "name": plugin.name,
                "version": _version(plugin.version),
                "format": plugin.format,
                "source": _source(item) if from_store else "config",
                "enabled": True,
                "skills": _skill_ids(plugin),
                "commands": [c.name for c in plugin.commands],
                "agents": [a.name for a in plugin.agents],
                "hooks": [
                    f"{event} {group.matcher or '*'}"
                    for event, groups in plugin.hooks.items()
                    for group in groups
                ],
                "mcp_servers": list(plugin.mcp_servers),
                "agents_off": bool(plugin.agents) and not subagents,
                "error": "",
            }
        )
    cards += _failed_cards(paths, plugins, installed)
    loaded = {c["name"] for c in cards}
    for item in installed.values():
        if item.name not in loaded and not item.enabled:
            cards.append(
                _bare_card(
                    item.name, item.version, _source(item), enabled=False
                )
            )
    return cards


class PluginCard(ServerCard):
    """Карточка плагина: Enter/Space/клик раскрывает состав."""

    def __init__(self, info: dict) -> None:
        super().__init__(info["name"], info)

    def _renderable(self) -> Text:
        info = self._info
        on = info["enabled"] and not info["error"]
        t = Text()
        t.append("▾ " if self._expanded else "▸ ", style=_DEEP_DIM)
        t.append("✓ " if on else "✗ ", style=_OK if on else _ERR)
        t.append(info["name"])
        meta = [info["version"], info["format"], info["source"]]
        t.append("    " + " · ".join(m for m in meta if m), style=_DIM)
        if info["error"]:
            t.append(f"    {info['error']}", style=_ERR)
        if self._expanded:
            for label, key in (
                ("skills", "skills"),
                ("commands", "commands"),
                ("agents", "agents"),
                ("hooks", "hooks"),
                ("mcp", "mcp_servers"),
            ):
                if info[key]:
                    t.append(
                        f"\n    {label}: {', '.join(info[key])}",
                        style=_DEEP_DIM,
                    )
            if info["agents_off"]:
                t.append(f"\n    {_AGENTS_NOTE}", style=_DIM)
        return t


class PluginsScreen(Screen):
    def __init__(self, *, settings=None) -> None:
        super().__init__()
        self.cards: list[PluginCard] = [
            PluginCard(info) for info in plugin_cards(settings)
        ]

    def on_mount(self) -> None:
        if self.cards:
            self.cards[0].focus()

    def on_key(self, event: events.Key) -> None:
        if event.key == "down":
            event.stop()
            self.focus_next()
        elif event.key == "up":
            event.stop()
            self.focus_previous()

    def _empty_renderable(self) -> Text | None:
        return None if self.cards else Text(_EMPTY, style=_DIM)

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static(
                    "plugins · плагины", id="header-left", markup=False
                )
            trace = VerticalScroll(id="trace")
            trace.can_focus = False
            with trace:
                empty = self._empty_renderable()
                if empty is not None:
                    yield Static(empty)
                else:
                    yield from self.cards
                yield Static(
                    Text(_HINT, style=_DEEP_DIM), classes="mcp-summary"
                )
            yield Static(_FOOTER, id="footer", markup=False)
