"""ViewerScreen — read-only просмотр (payload / файл / markdown)."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from gc_tui.messages import CopyBlockRequest
from gc_tui.services.clipboard_ops import copy_out
from gc_tui.services.json_payload import render_payload
from gc_tui.services.trace_render import build_block_widgets


class ViewerScreen(Screen):
    """Простой просмотрщик: заголовок + тело.

    content может быть:
    - dict/list → pretty-print JSON с solarized-dark подсветкой (через render_payload).
    - str: try json.loads → если ok, тоже подсветка; иначе plain Static.
    - str при ``markdown=True`` — Markdown тем же движком, что ответ в ленте:
      проза строками, код и таблицы панелями (тело скилла).
    """

    def __init__(
        self, *, title: str, content: Any, markdown: bool = False
    ) -> None:
        super().__init__()
        self._title = title
        self._content = content
        self._markdown = markdown

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static(self._title, id="header-left", markup=False)
            with VerticalScroll(id="trace"):
                body = self._body()
                # .viewer-body — для CSS-padding и query'ей в тестах.
                body.add_class("viewer-body")
                yield body
            yield Static(
                "↑/↓ / PgUp/PgDn — прокрутка · Esc — назад",
                id="footer",
                markup=False,
            )

    def _body(self):
        if not self._markdown:
            return render_payload(self._content)
        widgets = build_block_widgets(
            str(self._content), prose_bullet=False, event_no=0
        )
        return Vertical(*widgets, classes="viewer-markdown")

    def on_copy_block_request(self, event: CopyBlockRequest) -> None:
        """Кнопка ❐ панели кода или таблицы — копия наружу. Отложенную
        автокопию выделения от двойного клика отменяем, иначе она затрёт
        блок позже (тот же приём, что у превью)."""
        if event.payload:
            self.app.cancel_pending_select_copy()
            copy_out(self.app, event.payload, toast=True, blocking=False)
