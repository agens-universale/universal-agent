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
from gc_tui.palette import NOTE_INK as _NOTE_INK
from gc_tui.palette import OK as _OK
from gc_tui.palette import TOOL_NAME as _TOOL_NAME
from gc_tui.services.column_row import ColumnRow, ColumnRows
from gc_tui.services.plural import plural_form
from gc_tui.widgets.column_static import ColumnStatic

_FOOTER = (
    "↑/↓ — серверы · Enter — раскрыть · PgUp/PgDn — прокрутка · Esc — назад"
)

# Цвета — из tui.palette (единый источник). Принцип: цвет — редкий сигнал
# (глиф + статус), тело монохромно/приглушённо.


def _truncate(text: str, limit: int = 80) -> str:
    text = (text or "").strip().replace("\n", " ")
    return (text[: limit - 1] + "…") if len(text) > limit else text


_TOOL_DESC_LIMIT = 80  # описание тула в раскрытом списке — одна строка

# Сервер ещё не подключён, но и не отказал (спека §5.7): тулы записи
# (каталог) модели уже предложены. Не отказ — ни глифом, ни в итоге.
_WAITING_LABELS = {
    "connecting": ("подключается", "подключаются"),
    "cached": ("из каталога", "из каталога"),
}


class ServerCard(ColumnStatic):
    """Карточка MCP-сервера: фокусируемая, Enter/Space/клик раскрывает тулы.

    Рендер — Rich Text: маркер ▸/▾ ($deep-dim) + статус ✓/•/✗
    ($ok/$note-ink/$err) + имя. «•» — сервер подключается или его тулы
    из каталога. Раскрытие: connected, connecting и cached — список
    «короткое имя + описание» (префикс mcp_<server>_ срезан, колонка
    выровнена); failed — полная причина.
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

    def _renderable(self) -> ColumnRows:
        info = self._info
        status = info.get("status", "unknown")
        tools = info.get("tools") or []
        offered = status == "connected" or status in _WAITING_LABELS
        t = Text()
        t.append("▾ " if self._expanded else "▸ ", style=_DEEP_DIM)
        self._append_status(t, status, len(tools))
        desc = _truncate(info.get("description") or "")
        if desc:
            t.append(f"\n    · {desc}", style=_DEEP_DIM)
        body = self._body_rows(offered, tools) if self._expanded else []
        return ColumnRows([t, *body])

    def _append_status(self, t: Text, status: str, n_tools: int) -> None:
        """Глиф, имя и статус: подключён, ждёт (подключается или тулы из
        каталога), остальное — отказ с причиной."""
        if status == "connected":
            t.append("✓ ", style=_OK)
            t.append(self._name)  # имя — обычный $ink
            t.append(f"    {status} · {n_tools} тулов", style=_OK)
            return
        if status in _WAITING_LABELS:
            label = _WAITING_LABELS[status][0]
            t.append("• ", style=_NOTE_INK)
            t.append(self._name)
            tail = f" · {n_tools} тулов" if n_tools else ""
            t.append(f"    {label}{tail}", style=_NOTE_INK)
            return
        t.append("✗ ", style=_ERR)
        t.append(self._name)
        t.append(f"    {status}", style=_ERR)
        reason = self._info.get("reason")
        if reason:
            t.append(" — ", style=_DIM)
            t.append(_truncate(reason, 90), style=_DIM)

    def _body_rows(self, offered: bool, tools: list) -> list[ColumnRow | Text]:
        """Строки раскрытой карточки: описание тула переносится в своей
        колонке, под собой, а не под именем (дизайн-код §10)."""
        if offered and tools:
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
            return [
                ColumnRow(
                    Text(f"    {n.ljust(width)}", style=_TOOL_NAME),
                    [
                        Text(
                            f"  {_truncate(d, _TOOL_DESC_LIMIT)}",
                            style=_DEEP_DIM,
                        )
                    ]
                    if d
                    else [],
                )
                for n, d in shorts
            ]
        if not offered and self._info.get("reason"):
            full = str(self._info["reason"])
            return [Text(f"    {ln}", style=_DIM) for ln in full.splitlines()]
        return []


class McpScreen(Screen):
    def __init__(
        self, *, settings=None, tools_snapshot: dict | None = None
    ) -> None:
        super().__init__()
        self._snapshot = tools_snapshot  # {server: {status,reason,description,tools}} | None
        self.cards: list[ServerCard] = [
            ServerCard(name, info)
            for name, info in (tools_snapshot or {}).items()
        ]

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
        """Итог: подключены, ждут (каждое состояние — только если есть),
        отказы и тулы, предложенные модели (у ждущих — тулы каталога)."""
        counts = {"connected": 0, **dict.fromkeys(_WAITING_LABELS, 0)}
        n_fail = n_tools = 0
        for info in self._snapshot.values():
            info = info or {}
            status = info.get("status")
            if status in counts:
                counts[status] += 1
                n_tools += len(info.get("tools") or [])
            else:
                n_fail += 1
        t = Text()
        t.append("─" * 40 + "\n", style=_DEEP_DIM)
        t.append("итого: ", style=_DIM)
        t.append(f"{counts['connected']} connected", style=_OK)
        for status, (one, many) in _WAITING_LABELS.items():
            n = counts[status]
            if n:
                t.append(" · ", style=_DIM)
                label = plural_form(n, one, many, many)
                t.append(f"{n} {label}", style=_NOTE_INK)
        t.append(" · ", style=_DIM)
        t.append(f"{n_fail} failed", style=_ERR if n_fail else _DIM)
        t.append(f" · {n_tools} тулов", style=_DIM)
        return t

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static(
                    "mcp · серверы и тулы", id="header-left", markup=False
                )
            trace = VerticalScroll(id="trace")
            trace.can_focus = False  # фокус живёт на карточках; контейнеру он
            # не нужен (рамка фокуса у #trace снята в styles.tcss для всех)
            with trace:
                empty = self._empty_renderable()
                if empty is not None:
                    yield Static(empty)
                else:
                    yield from self.cards
                    yield Static(
                        self._summary_renderable(), classes="mcp-summary"
                    )
            yield Static(_FOOTER, id="footer", markup=False)
