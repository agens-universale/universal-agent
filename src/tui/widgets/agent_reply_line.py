"""AgentReplyLine — Static с event_no для не-прозаических блоков агентских реплик.

Multi-widget рендер kind=agent (см. trace_render.build_block_widgets) монтирует
несколько виджетов на одно событие: проза идёт как TraceRow, а code-block-панели
и markdown-таблицы — как AgentReplyLine. Привязка event_no нужна, чтобы Ctrl+клик
по любому блоку открыл payload родительского события — иначе click буллит
до ближайшего кликабельного соседа (другой event) и Viewer показывает не тот
payload.
"""
from __future__ import annotations

from typing import Optional

from textual.message import Message
from textual.widgets import Static

from tui.widgets.click_model import ClickModelMixin


class AgentReplyLine(ClickModelMixin, Static):
    """Static с привязанным event_no. Ctrl+клик → OpenEventRequest, plain-клик
    (если focusable) → self.focus().

    focusable=False по дефолту (как у Static). trace_render.build_block_widgets
    передаёт focusable=True первому виджету события (Tab-стоп) — в т.ч. когда
    это AgentReplyLine (событие начинается с code-block или таблицы)."""

    def __init__(self, content, *, event_no: int,
                 focusable: bool = False, copy_text: str | None = None,
                 **kwargs) -> None:
        super().__init__(content, **kwargs)
        self._event_no = event_no
        self.can_focus = focusable
        # None → штатный Static (rich.Text-путь, нативный partial select).
        # Не-None → get_selection отдаёт весь markdown/fenced, игнорируя координаты
        # (rich.Table/rich.Syntax = RichVisual, координаты не мапятся на markdown).
        self._copy_text = copy_text

    def get_selection(self, selection) -> "tuple[str, str] | None":
        if self._copy_text is not None:
            return self._copy_text, "\n"
        return super().get_selection(selection)

    def selection_updated(self, selection) -> None:
        # Whole-widget highlight: RichVisual не красит selection сам (в отличие
        # от Text/Content). Класс только при copy_text — иначе Text-ветка уже
        # имеет нативный посимвольный span. Screen._watch_selections уже зовёт
        # selection_updated на enter/leave selection.
        if self._copy_text is not None:
            self.set_class(selection is not None, "-copy-selected")
        super().selection_updated(selection)

    def _open_request(self) -> Optional[Message]:
        if not self._event_no:
            return None
        from tui.messages import OpenEventRequest
        return OpenEventRequest(self._event_no)
