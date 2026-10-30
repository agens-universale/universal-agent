"""PluginsScreen — установленные плагины (read-only). Карточки как в /mcp."""

from __future__ import annotations

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
from gc_tui.palette import WARN as _WARN
from gc_tui.screens.mcp import ServerCard
from gc_tui.services.plugin_cards import general_notices, plugin_cards
from gc_tui.services.skills_scan import run_off_loop

_FOOTER = "[Enter] раскрыть  [↑↓] плагины  [Esc] back"
_HINT = "Управление: gc-plugin install | enable | disable | uninstall"
_EMPTY = "Плагинов нет. Установка: gc-plugin install <owner/repo>"


def _warning_count(info: dict) -> int:
    """Сколько строк карточки — неполадки (сведения не считаются)."""
    return (
        len(info["warnings"])
        + sum(len(r["notes"]) for r in info["roles"])
        + sum(not r["ok"] for r in info["roles"])
    )


def _append_roles(t: Text, roles: list[dict]) -> None:
    for role in roles:
        t.append(
            f"\n    роль {role['name']}: {role['status']}",
            style=_DEEP_DIM if role["ok"] else _WARN,
        )
        for note in role["notes"]:
            t.append(f"\n      {note}", style=_WARN)


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
        warnings = _warning_count(info)
        if warnings and not self._expanded:
            t.append(f"    ⚠ {warnings}", style=_WARN)
        if self._expanded:
            self._append_details(t)
        return t

    def _append_details(self, t: Text) -> None:
        info = self._info
        for label, key in (
            ("skills", "skills"),
            ("commands", "commands"),
            ("hooks", "hooks"),
            ("mcp", "mcp_servers"),
        ):
            if info[key]:
                t.append(
                    f"\n    {label}: {', '.join(info[key])}",
                    style=_DEEP_DIM,
                )
        _append_roles(t, info["roles"])
        for line in info["warnings"]:
            t.append(f"\n    ⚠ {line}", style=_WARN)
        for line in info["info"]:
            t.append(f"\n    · {line}", style=_DIM)


class PluginsScreen(Screen):
    """Карточки строятся после показа экрана: чтение диска и обход ролей
    (доступность, права) — вне цикла интерфейса, один раз на открытие."""

    def __init__(self, *, settings=None) -> None:
        super().__init__()
        self._settings = settings
        self.cards: list[PluginCard] = []

    def on_mount(self) -> None:
        self.run_worker(self._load, exclusive=True)

    async def _load(self) -> None:
        settings = self._settings
        cards, notices = await run_off_loop(
            lambda: (plugin_cards(settings), general_notices(settings)),
            "plugin-cards",
        )
        await self._show(cards, notices)

    async def _show(self, cards: list[dict], notices: list[str]) -> None:
        self.cards = [PluginCard(info) for info in cards]
        widgets: list[Static] = [
            Static(Text(f"⚠ {n}", style=_WARN), classes="mcp-summary")
            for n in notices
        ]
        widgets += self.cards or [Static(Text(_EMPTY, style=_DIM))]
        trace = self.query_one("#trace", VerticalScroll)
        await trace.mount_all(widgets, before=0)
        if self.cards:
            self.cards[0].focus()

    def on_key(self, event: events.Key) -> None:
        if event.key == "down":
            event.stop()
            self.focus_next()
        elif event.key == "up":
            event.stop()
            self.focus_previous()

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static(
                    "plugins · плагины", id="header-left", markup=False
                )
            trace = VerticalScroll(id="trace")
            trace.can_focus = False
            with trace:
                yield Static(
                    Text(_HINT, style=_DEEP_DIM), classes="mcp-summary"
                )
            yield Static(_FOOTER, id="footer", markup=False)
