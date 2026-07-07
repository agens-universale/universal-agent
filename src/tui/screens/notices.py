"""NoticesScreen — список уведомлений: стартовые + runtime (read-only, F4)."""
from __future__ import annotations

from typing import List

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from startup_notices import Notice
from tui import palette

_FOOTER = "/config · /mcp  [Esc] назад"
_WARN_GLYPH = "⚠"
_INFO_GLYPH = "ℹ"
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
    t.append(_glyph(n.level) + " ",
             style=_WARN_STYLE if n.level == "warning" else _INFO_STYLE)
    t.append(n.message)
    if n.highlights:
        t.highlight_words(n.highlights, style=_NAME_STYLE)
    return t


class NoticesScreen(Screen):
    """Экран уведомлений — зеркало McpScreen (header/footer/Esc/scroll)."""

    def __init__(self, notices: List[Notice] | None = None) -> None:
        super().__init__()
        self._notices: List[Notice] = notices if notices is not None else []

    def _header_text(self) -> str:
        warns = sum(1 for n in self._notices if n.level == "warning")
        infos = sum(1 for n in self._notices if n.level == "info")
        parts = []
        if warns:
            parts.append(f"{_WARN_GLYPH} {warns}")
        if infos:
            parts.append(f"{_INFO_GLYPH} {infos}")
        return "уведомления" + (" · " + " · ".join(parts) if parts else "")

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static(self._header_text(), id="header-left", markup=False)
            trace = VerticalScroll(id="trace")
            trace.can_focus = False
            with trace:
                if not self._notices:
                    yield Static("Уведомлений нет", markup=False)
                else:
                    # Пустая строка-разделитель между уведомлениями (не после последнего).
                    for i, n in enumerate(self._notices):
                        if i:
                            yield Static("")
                        yield Static(_notice_text(n))
            yield Static(_FOOTER, id="footer", markup=False)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.app.pop_screen()
