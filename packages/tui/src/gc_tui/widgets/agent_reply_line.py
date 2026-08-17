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

from gc_tui.widgets.click_model import ClickModelMixin


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
        # Renderable для in-place подсветки (Syntax.bg); None если copy_text нет.
        self._select_renderable = content if copy_text is not None else None

    def get_selection(self, selection) -> "tuple[str, str] | None":
        if self._copy_text is not None:
            return self._copy_text, "\n"
        return super().get_selection(selection)

    def selection_updated(self, selection) -> None:
        # Whole-widget highlight: RichVisual не красит selection сам.
        # 1) CSS-класс — background виджета (table padding / пустые зоны).
        # 2) Syntax — свой bgcolor в каждой ячейке; CSS его не перекрывает,
        #    поэтому меняем background_color in-place (BAND_BG ↔ FOCUS_BG).
        # Outline не используем: margin-left у .code-indent клипает левый край.
        if self._copy_text is not None:
            selected = selection is not None
            self.set_class(selected, "-copy-selected")
            self._apply_syntax_select_bg(selected)
        super().selection_updated(selection)

    def _apply_syntax_select_bg(self, selected: bool) -> None:
        from rich.style import Style
        from rich.syntax import Syntax

        from gc_tui.palette import BAND_BG, FOCUS_BG

        renderable = self._select_renderable
        if not isinstance(renderable, Syntax):
            return
        bg = FOCUS_BG if selected else BAND_BG
        if renderable.background_color == bg:
            return
        renderable.background_color = bg
        renderable.background_style = Style(bgcolor=bg)
        # Пересобрать visual без reflow (размер тот же).
        self.update(renderable, layout=False)

    def _open_request(self) -> Optional[Message]:
        if not self._event_no:
            return None
        from gc_tui.messages import OpenEventRequest
        return OpenEventRequest(self._event_no)
