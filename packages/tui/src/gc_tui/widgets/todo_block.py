"""TodoBlock — чек-лист session_todo как ОДНА единица выделения в трейсе.

Зачем контейнер, а не просто список строк: пункты рендерятся отдельными
``TraceRow`` (каждому нужен свой префикс с глифом, чтобы длинный текст
переносился под собственное начало — hanging indent, spec v4). Но событие
в трейсе одно, значит и выделяться оно должно целиком.

Строки внутри не фокусируемы; Textual на MouseDown поднимается до
фокусируемого предка (``Screen.get_focusable_widget_at``), поэтому клик по
любому пункту подсвечивает весь блок, а Tab-цикл сохраняет контракт
«1 событие = 1 Tab-стоп». Ctrl+клик работает с любой строки — ``event_no``
проставлен и строкам, и блоку.
"""

from __future__ import annotations

from typing import Optional, Sequence

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget

from gc_tui.widgets.click_model import ClickModelMixin


class TodoBlock(ClickModelMixin, Widget):
    """Контейнер строк чек-листа. Визуал строк — в rich.Text (todo_render),
    воздух вокруг блока и подсветка фокуса — в styles.tcss."""

    DEFAULT_CSS = """
    TodoBlock {
        height: auto;
        width: 1fr;
        layout: vertical;
        padding: 0;
    }
    """
    can_focus = True

    def __init__(
        self, rows: Sequence[Widget], *, event_no: int = 0, **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._rows = list(rows)
        self._event_no = event_no

    @property
    def todo_rows(self) -> list:
        """Строки блока (заголовок + пункты) — для тестов и отладки."""
        return list(self._rows)

    def compose(self) -> ComposeResult:
        yield from self._rows

    def _open_request(self) -> Optional[Message]:
        if not self._event_no:
            return None
        from gc_tui.messages import OpenEventRequest

        return OpenEventRequest(self._event_no)
