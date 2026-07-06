"""AgentReplyLine — Static с event_no для не-прозаических блоков агентских реплик.

Multi-widget рендер kind=agent (см. trace_render.build_block_widgets) монтирует
несколько виджетов на одно событие: проза идёт как TraceRow, а code-block-панели
и markdown-таблицы — как AgentReplyLine. Привязка event_no нужна, чтобы двойной
клик по любому блоку открыл payload родительского события — иначе click буллит
до ближайшего кликабельного соседа (другой event) и Viewer показывает не тот
payload.
"""
from __future__ import annotations

from textual import events
from textual.widgets import Static


class AgentReplyLine(Static):
    """Static с привязанным event_no. Двойной клик → OpenEventRequest.

    focusable=False по дефолту (как у Static). trace_render.build_block_widgets
    передаёт focusable=True первому виджету события (Tab-стоп) — в т.ч. когда
    это AgentReplyLine (событие начинается с code-block или таблицы)."""

    FOCUS_ON_CLICK = False   # клик не фокусирует строку; навигация — только Tab/Shift+Tab

    def __init__(self, content, *, event_no: int,
                 focusable: bool = False, **kwargs) -> None:
        super().__init__(content, **kwargs)
        self._event_no = event_no
        self.can_focus = focusable

    def on_click(self, event: events.Click) -> None:
        if event.chain == 2 and self._event_no:
            from tui.messages import OpenEventRequest
            self.post_message(OpenEventRequest(self._event_no))
