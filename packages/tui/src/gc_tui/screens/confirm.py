"""ConfirmScreen — модальный confirm для подтверждения опасных действий.

Навигация: ←/→ переключают вариант, Enter выбирает подсвеченный, Esc — отмена.
Левый вариант (affirmative) выбран по умолчанию. y/n работают как прямые шорткаты.
"""
from __future__ import annotations

import re
from typing import Optional

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from gc_tui.palette import ACCENT, BG, BRIGHT, DIM, INK

# Парсер вариантов из hint: «[K] подпись  [K] подпись» → [(keycap, label), ...].
# Подпись — всё до следующего «[» или конца строки, без хвостовых пробелов.
_OPT_RE = re.compile(r"(\[[^\]]+\])\s*([^\[]*?)(?=\s*\[|\s*$)")


def _render_hint(hint: str, selected: int) -> Text:
    """Рендер вариантов; подсвеченный — teal-«кнопка», прочие — keycap accent + текст dim."""
    opts = _OPT_RE.findall(hint)
    t = Text()
    for idx, (key, label) in enumerate(opts):
        if idx > 0:
            t.append("   ")  # разделитель между кнопками
        if idx == selected:
            t.append(f" {key} {label} ", style=f"bold {BG} on {ACCENT}")
        else:
            t.append(f" {key}", style=ACCENT)
            t.append(f" {label} ", style=DIM)
    return t


class ConfirmScreen(ModalScreen[bool]):
    """Модальное окно подтверждения. Возвращает True/False через dismiss(value).

    Вариант 0 (левый) — affirmative (True); вариант 1 — negative (False).
    """

    DEFAULT_CSS = f"""
    ConfirmScreen {{ align: center middle; }}
    #confirm-dialog {{
        background: #161A23;
        border: round {ACCENT};
        border-title-color: {BRIGHT};
        border-title-style: bold;
        padding: 1 2;
        width: 56;
        height: auto;
    }}
    #confirm-message {{ color: {INK}; }}
    """

    def __init__(
        self,
        *,
        message: str,
        hint: str = "[Y] да  [N] нет",
        title: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._message = message
        self._hint = hint
        self._title = title
        self._options = _OPT_RE.findall(hint)
        self._selected = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(self._message, id="confirm-message", markup=False)
            yield Static("", id="confirm-spacer")
            yield Static(_render_hint(self._hint, self._selected), id="confirm-hint")

    def on_mount(self) -> None:
        if self._title:
            self.query_one("#confirm-dialog").border_title = self._title

    def _refresh_hint(self) -> None:
        self.query_one("#confirm-hint", Static).update(
            _render_hint(self._hint, self._selected)
        )

    def on_key(self, event: events.Key) -> None:
        key = event.key
        if key == "left":
            if self._selected > 0:
                self._selected -= 1
                self._refresh_hint()
            event.stop()
        elif key == "right":
            if self._selected < len(self._options) - 1:
                self._selected += 1
                self._refresh_hint()
            event.stop()
        elif key == "enter":
            self.dismiss(self._selected == 0)
            event.stop()
        elif key in ("y", "Y"):
            self.dismiss(True)
            event.stop()
        elif key in ("n", "N", "escape"):
            self.dismiss(False)
            event.stop()
