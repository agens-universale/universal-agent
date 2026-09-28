"""ViewerScreen — read-only просмотр (payload / файл / markdown)."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from gc_tui.services.json_payload import render_payload


class ViewerScreen(Screen):
    """Простой просмотрщик: заголовок + тело.

    content может быть:
    - dict/list → pretty-print JSON с solarized-dark подсветкой (через render_payload).
    - str: try json.loads → если ok, тоже подсветка; иначе plain Static.
    """

    def __init__(self, *, title: str, content: Any) -> None:
        super().__init__()
        self._title = title
        self._content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static(self._title, id="header-left", markup=False)
            with VerticalScroll(id="trace"):
                body = render_payload(self._content)
                # .viewer-body — для CSS-padding и query'ей в тестах.
                body.add_class("viewer-body")
                yield body
            yield Static(
                "[Esc] back  [↑↓ / PgUp PgDn] прокрутка",
                id="footer",
                markup=False,
            )
