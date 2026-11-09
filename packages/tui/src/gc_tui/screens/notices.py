"""NoticesScreen — список уведомлений: стартовые + runtime (read-only, F4/notices)."""

from __future__ import annotations

from typing import List

from gc_core.runtime_notices import mark_runtime_notices_seen
from gc_core.startup_notices import Notice
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from gc_tui import palette
from gc_tui.services import notices_ui

_FOOTER = "/config · /mcp · Esc — назад"
# Значки — из общего источника (tui.services.notices_ui): язык этого экрана и бейджа
# обязан совпадать, а свои копии уже разъезжались по вариационному селектору.
_WARN_GLYPH = notices_ui.WARN_GLYPH
_INFO_GLYPH = notices_ui.INFO_GLYPH
# Стили из палитры (единый источник): имена — цвет инлайн-кода/пути в трейсе
# ($code = palette.CODE) → единый стиль тех. имён по всему UI.
_WARN_STYLE = palette.WARN
_INFO_STYLE = palette.INFO
_NAME_STYLE = palette.CODE


def _glyph(level: str) -> str:
    return _WARN_GLYPH if level == "warning" else _INFO_GLYPH


def _notice_text(n: Notice) -> Text:
    """Одно уведомление: цветной глиф + текст с выделенными тех. именами."""
    t = Text()
    t.append(
        _glyph(n.level) + " ",
        style=_WARN_STYLE if n.level == "warning" else _INFO_STYLE,
    )
    t.append(n.message)
    if n.highlights:
        t.highlight_words(n.highlights, style=_NAME_STYLE)
    return t


class NoticesScreen(Screen):
    """Экран уведомлений — зеркало McpScreen (header/footer/Esc/scroll)."""

    def __init__(self, notices: List[Notice] | None = None) -> None:
        super().__init__()
        # notices в конструкторе — для тестов; в проде список собирается в on_mount.
        self._initial_notices = notices

    def _load_notices(self) -> List[Notice]:
        collect = getattr(self.app, "collect_notices", None)
        if callable(collect):
            return collect()
        return list(self._initial_notices or [])

    def _header_text(self, notices: List[Notice]) -> str:
        warns = sum(1 for n in notices if n.level == "warning")
        infos = sum(1 for n in notices if n.level == "info")
        parts = []
        if warns:
            parts.append(f"{_WARN_GLYPH} {warns}")
        if infos:
            parts.append(f"{_INFO_GLYPH} {infos}")
        # Счётчики — пара, между ними хватает пробела; точка отделяет их от названия
        # экрана (тот же язык, что у бейджа: format_notices_badge). Внутри пары
        # пробел, между парами двойной — язык обязан совпадать с бейджем дословно.
        return "уведомления" + (" · " + "  ".join(parts) if parts else "")

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static("уведомления", id="header-left", markup=False)
            yield VerticalScroll(id="trace")
            yield Static(_FOOTER, id="footer", markup=False)

    def on_mount(self) -> None:
        # После просмотра гасим бейджи "новых" runtime-уведомлений.
        mark_runtime_notices_seen()
        self._render_notices()

    def _render_notices(self) -> None:
        notices = self._load_notices()
        self.query_one("#header-left", Static).update(
            self._header_text(notices)
        )
        trace = self.query_one("#trace", VerticalScroll)
        trace.remove_children()
        if not notices:
            trace.mount(Static("Уведомлений нет", markup=False))
            return
        for i, n in enumerate(notices):
            if i:
                trace.mount(Static(""))
            trace.mount(Static(_notice_text(n)))

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.app.pop_screen()
