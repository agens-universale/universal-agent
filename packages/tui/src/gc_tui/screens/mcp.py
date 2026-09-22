"""McpScreen — статус MCP-серверов (read-only, снимок сессии). Карточки."""

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
from gc_tui.palette import TOOL_NAME as _TOOL_NAME

_FOOTER = "[Enter] раскрыть  [↑↓] серверы  [PgUp PgDn] прокрутка  [Esc] back"

# Цвета — из tui.palette (единый источник). Принцип: цвет — редкий сигнал
# (глиф + статус), тело монохромно/приглушённо.


def _truncate(text: str, limit: int = 80) -> str:
    text = (text or "").strip().replace("\n", " ")
    return (text[: limit - 1] + "…") if len(text) > limit else text


_TOOL_DESC_LIMIT = 80  # описание тула в раскрытом списке — одна строка


class ServerCard(Static):
    """Карточка MCP-сервера: фокусируемая, Enter/Space/клик раскрывает тулы.

    Рендер — Rich Text: маркер ▸/▾ ($deep-dim) + статус ✓/✗ ($ok/$err) + имя.
    Раскрытие: connected — список «короткое имя + описание» (префикс
    mcp_<server>_ срезан, колонка выровнена); failed — полная причина.
    """

    can_focus = True

    def __init__(self, name: str, info: dict | None) -> None:
        super().__init__(markup=False)
        self.add_class("mcp-card")
        self._name = name
        self._info = info or {}
        self._expanded = False

    @property
    def expanded(self) -> bool:
        return self._expanded

    def toggle(self) -> None:
        self._expanded = not self._expanded
        # вне app (unit-тесты) Static.update недоступен — обновляем только смонтированными
        if self.is_mounted:
            self.update(self._renderable())

    def on_mount(self) -> None:
        self.update(self._renderable())

    def on_key(self, event: events.Key) -> None:
        if event.key in ("enter", "space"):
            event.stop()
            event.prevent_default()
            self.toggle()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        event.prevent_default()
        self.focus()
        self.toggle()

    def on_focus(self) -> None:
        self.scroll_visible(animate=False)

    # ---- рендер ------------------------------------------------------

    def _renderable(self) -> Text:
        info = self._info
        status = info.get("status", "unknown")
        connected = status == "connected"
        tools = info.get("tools") or []
        t = Text()
        t.append("▾ " if self._expanded else "▸ ", style=_DEEP_DIM)
        t.append("✓ " if connected else "✗ ", style=_OK if connected else _ERR)
        t.append(self._name)  # имя — обычный $ink
        t.append("    ")
        if connected:
            t.append(f"{status} · {len(tools)} тулов", style=_OK)
        else:
            t.append(status, style=_ERR)
            reason = info.get("reason")
            if reason:
                t.append(" — ", style=_DIM)
                t.append(_truncate(reason, 90), style=_DIM)
        desc = _truncate(info.get("description") or "")
        if desc:
            t.append(f"\n    · {desc}", style=_DEEP_DIM)
        if self._expanded:
            self._append_body(t, connected, tools)
        return t

    def _append_body(self, t: Text, connected: bool, tools: list) -> None:
        if connected and tools:
            prefix = f"mcp_{self._name}_"
            shorts = [
                (
                    (tt.get("name") or "")[len(prefix) :]
                    if (tt.get("name") or "").startswith(prefix)
                    else (tt.get("name") or ""),
                    (tt.get("description") or ""),
                )
                for tt in tools
            ]
            width = max(len(n) for n, _ in shorts)
            for n, d in shorts:
                t.append(f"\n    {n.ljust(width)}", style=_TOOL_NAME)
                if d:
                    t.append(f"  {_truncate(d, _TOOL_DESC_LIMIT)}", style=_DEEP_DIM)
        elif not connected and self._info.get("reason"):
            full = str(self._info["reason"])
            for ln in full.splitlines():
                t.append(f"\n    {ln}", style=_DIM)


class McpScreen(Screen):
    def __init__(self, *, config: dict | None = None, tools_snapshot: dict | None = None) -> None:
        super().__init__()
        self._snapshot = tools_snapshot  # {server: {status,reason,description,tools}} | None
        self.cards: list[ServerCard] = [ServerCard(name, info) for name, info in (tools_snapshot or {}).items()]

    def on_mount(self) -> None:
        if self.cards:
            self.cards[0].focus()

    def on_key(self, event: events.Key) -> None:
        # up/down — в on_key, не в BINDINGS: хендлеры всплытия срабатывают
        # раньше биндингов VerticalScroll, иначе при переполнении контейнер
        # съедает стрелки на скролл и фокус-навигация ломается.
        if event.key == "down":
            event.stop()
            self.focus_next()
        elif event.key == "up":
            event.stop()
            self.focus_previous()

    # ---- рендер вспомогательных блоков --------------------------------

    def _empty_renderable(self) -> Text | None:
        if self._snapshot is None:
            return Text("# статус появится после первого запуска", style=_DIM)
        if not self._snapshot:
            return Text("# нет MCP-серверов", style=_DIM)
        return None

    def _summary_renderable(self) -> Text:
        n_conn = n_fail = n_tools = 0
        for info in self._snapshot.values():
            info = info or {}
            if info.get("status") == "connected":
                n_conn += 1
                n_tools += len(info.get("tools") or [])
            else:
                n_fail += 1
        t = Text()
        t.append("─" * 40 + "\n", style=_DEEP_DIM)
        t.append("итого: ", style=_DIM)
        t.append(f"{n_conn} connected", style=_OK)
        t.append(" · ", style=_DIM)
        t.append(f"{n_fail} failed", style=_ERR if n_fail else _DIM)
        t.append(f" · {n_tools} тулов", style=_DIM)
        return t

    def rendered_text(self) -> str:
        empty = self._empty_renderable()
        if empty is not None:
            return empty.plain
        parts = [card._renderable().plain for card in self.cards]
        parts.append(self._summary_renderable().plain)
        return "\n\n".join(parts)

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static("mcp · серверы и тулы", id="header-left", markup=False)
            trace = VerticalScroll(id="trace")
            trace.can_focus = False  # фокус живёт на карточках; глобальный
            # #trace:focus-outline рисуется поверх контента — здесь не нужен
            with trace:
                empty = self._empty_renderable()
                if empty is not None:
                    yield Static(empty)
                else:
                    yield from self.cards
                    yield Static(self._summary_renderable(), classes="mcp-summary")
            yield Static(_FOOTER, id="footer", markup=False)
