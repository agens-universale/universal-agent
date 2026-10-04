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

from gc_tui.messages import CopyBlockRequest, OpenImageRequest
from gc_tui.services.clipboard_ops import copy_out
from gc_tui.services.image_open import open_image
from gc_tui.services.skill_view import truncate_path_left
from gc_tui.services.trace_retention import subagent_parent_ids_in_window

_PATH_MAXLEN = 48


class PreviewScreen(Screen[bool]):
    # priority=True: Enter/Esc срабатывают ДО фокусной строки. Иначе focusable
    # ToolCallRow/TraceRow перехватывают Enter (open-detail + stop) и тред не
    # открывается. В превью нет ввода — Enter всегда = открыть тред, Esc = назад.
    BINDINGS = [
        Binding("enter", "resume", "продолжить сессию", priority=True),
        Binding("escape", "back", "назад", priority=True),
    ]

    def __init__(
        self,
        *,
        segments: list,
        thread_id: str = "",
        project_path: str = "",
        subagent_records: list | None = None,
    ) -> None:
        super().__init__()
        self._segments = segments
        self._thread_id = thread_id
        self._project_path = project_path
        self._subagent_records = subagent_records

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static(
                    f"превью · сессия {self._thread_id[:8]}",
                    id="header-left",
                    markup=False,
                )
                yield Static(
                    truncate_path_left(self._project_path, _PATH_MAXLEN),
                    id="header-right",
                    markup=False,
                )
            yield VerticalScroll(id="trace")
            yield Static(
                "[Esc] назад  ·  [Enter] продолжить сессию  ·  [↑↓ / PgUp PgDn] прокрутка",
                id="preview-footer",
                markup=False,
            )

    def on_mount(self) -> None:
        from gc_tui.services.trace_render import (
            merge_nested_readonly,
            render_segments_readonly,
        )

        trace = self.query_one("#trace", VerticalScroll)
        segments = self._segments
        records = self._subagent_records
        calls = render_segments_readonly(trace, segments)
        if records:
            merge_nested_readonly(
                trace,
                records,
                calls,
                allowed_parent_ids=subagent_parent_ids_in_window(segments),
            )
        # Сырьё сегментов и sidecar после монтажа не держим: в виджетах уже
        # короткий трейс, тяжёлые tool output / nested вне окна не нужны.
        self._segments = []
        self._subagent_records = []

    def action_resume(self) -> None:
        self.dismiss(True)

    def action_back(self) -> None:
        self.dismiss(False)

    def on_summary_toggle_request(self, message) -> None:
        """Клик по маркеру сжатия → раскрыть/свернуть тело сводки (маркер
        non-focusable — Enter отдан резюму треда). Тело находим по event_no."""
        from gc_tui.widgets.trace_row import SummaryMarker

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

    def on_open_image_request(self, message: OpenImageRequest) -> None:
        """Клик по картинке в реплике превью — как на главном экране; футера нет, поэтому
        отказ — уведомлением."""
        refusal = open_image(message.path)
        if refusal:
            self.notify(refusal, severity="warning")

    def on_copy_block_request(self, event: CopyBlockRequest) -> None:
        """Копия наружу без буфера сессии: превью показывает ЧУЖОЙ тред, а
        _push_session_copy_buffer пишет в активную сессию главного экрана.

        Двойной клик перед этим сам рождает TextSelected и ставит в очередь
        отложенную автокопию видимого — отменяем, иначе она затрёт payload
        позже (гонка copy-on-select)."""
        if event.payload:
            self.app.cancel_pending_select_copy()
            copy_out(self.app, event.payload, toast=True, blocking=False)
