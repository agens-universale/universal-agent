"""PreviewScreen — read-only превью сессии реальными виджетами трейса.

Esc → dismiss(False) (назад). Enter → dismiss(True) (продолжить сессию).
Шапка: слева «превью · сессия <id>», справа — путь проекта. Футер — подсказки клавиш.
Промпт сессии виден в самом трейсе ниже, поэтому в шапку не дублируется."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from tui.services.skill_view import truncate_path_left

_PATH_MAXLEN = 48


class PreviewScreen(Screen[bool]):
    # priority=True: Enter/Esc срабатывают ДО фокусной строки. Иначе focusable
    # ToolCallRow/TraceRow перехватывают Enter (open-detail + stop) и тред не
    # открывается. В превью нет ввода — Enter всегда = открыть тред, Esc = назад.
    BINDINGS = [
        Binding("enter", "resume", "продолжить сессию", priority=True),
        Binding("escape", "back", "назад", priority=True),
    ]

    def __init__(self, *, segments: list, thread_id: str = "",
                 project_path: str = "") -> None:
        super().__init__()
        self._segments = segments
        self._thread_id = thread_id
        self._project_path = project_path

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static(f"превью · сессия {self._thread_id[:8]}",
                             id="header-left", markup=False)
                yield Static(truncate_path_left(self._project_path, _PATH_MAXLEN),
                             id="header-right", markup=False)
            yield VerticalScroll(id="trace")
            yield Static(
                "[Esc] назад  ·  [Enter] продолжить сессию  ·  [↑↓ / PgUp PgDn] прокрутка",
                id="preview-footer", markup=False)

    def on_mount(self) -> None:
        from tui.services.trace_render import render_segments_readonly
        render_segments_readonly(self.query_one("#trace", VerticalScroll), self._segments)

    def action_resume(self) -> None:
        self.dismiss(True)

    def action_back(self) -> None:
        self.dismiss(False)

    def on_summary_toggle_request(self, message) -> None:
        """Клик по маркеру сжатия → раскрыть/свернуть тело сводки (маркер
        non-focusable — Enter отдан резюму треда). Тело находим по event_no."""
        from tui.widgets.trace_row import SummaryMarker
        body = marker = None
        for w in self.query(SummaryMarker):
            if getattr(w, "_event_no", None) != message.event_no:
                continue
            if w.has_class("summary-body"):
                body = w
            elif w.has_class("summary-marker"):
                marker = w
        if body is None:
            return
        if body.has_class("hidden"):
            body.remove_class("hidden")
            if marker is not None:
                marker.set_prefix("▾ ")
            self.call_after_refresh(body.scroll_visible, animate=False)
        else:
            body.add_class("hidden")
            if marker is not None:
                marker.set_prefix("▸ ")
