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

from gc_tui.messages import BlockToggle, CopyBlockRequest, OpenImageRequest
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
        mcp_snapshot: dict | None = None,
    ) -> None:
        super().__init__()
        self._segments = segments
        self._thread_id = thread_id
        self._project_path = project_path
        self._subagent_records = subagent_records
        # Снимок MCP треда (meta.runtime.mcp_tools) — подписи MCP-вызовов.
        self._mcp_snapshot = mcp_snapshot
        # Блоки вызовов превью (P4): исполнители уровней и маршрут тогла.
        self._blocks = None

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
        from gc_core.constants.tool_display import (
            mcp_servers_of,
            mcp_tools_from_snapshot,
        )

        from gc_tui.services.trace_render import (
            merge_nested_readonly,
            render_segments_readonly,
        )

        trace = self.query_one("#trace", VerticalScroll)
        segments = self._segments
        records = self._subagent_records
        self._blocks = self._preview_blocks()
        opts = {
            "mcp_tools": mcp_tools_from_snapshot(self._mcp_snapshot),
            "mcp_servers": mcp_servers_of(self._mcp_snapshot),
            # корень из шапки превью: пути в подписях вызовов — от него
            "root": self._project_path or None,
            "blocks": self._blocks,
        }
        calls = render_segments_readonly(trace, segments, **opts)
        if records:
            merge_nested_readonly(
                trace,
                records,
                calls,
                allowed_parent_ids=subagent_parent_ids_in_window(segments),
                **opts,
            )
        # Сырьё сегментов и sidecar после монтажа не держим: в виджетах уже
        # короткий трейс, тяжёлые tool output / nested вне окна не нужны.
        self._segments = []
        self._subagent_records = []

    def _preview_blocks(self):
        """Блоки превью с журналом правок треда (только чтение; лимиты —
        `edits_view.diff_limits`) и долями из `agent_edits` (спека P4
        §9.10а): правки и доли — как вживую."""
        from gc_tui.services.block_edits import edit_settings
        from gc_tui.services.edits_lookup import lookup_for_settings
        from gc_tui.widgets.tool_blocks_view import PreviewBlocks

        settings = getattr(self.app, "base_settings", None)
        if settings is None:
            # Хост без настроек и тредов (превью вне приложения) — доли по
            # умолчанию, без журнала правок.
            return PreviewBlocks()
        cfg = settings()
        preview, window = edit_settings(cfg.settings.agent_edits.model_dump())
        lookup = None
        threads_dir = getattr(self.app, "_threads_dir", None)
        system_id = getattr(self.app, "_system", None)
        if threads_dir and system_id and self._thread_id:
            lookup = lookup_for_settings(
                threads_dir, system_id, self._thread_id, cfg
            )
        return PreviewBlocks(
            edits_lookup=lookup, diff_preview=preview, expanded_turns=window
        )

    def on_block_toggle(self, message: BlockToggle) -> None:
        """Пробел на строке блока и клик по фразе — раскрыть / свернуть
        блок своего уровня (Enter остаётся «открыть тред»)."""
        message.stop()
        if self._blocks is not None:
            self._blocks.views.route(message)

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
        expand = body.has_class("hidden")
        body.set_class(not expand, "hidden")
        if marker is not None:
            marker.set_expanded(expand)
        if expand:
            self.call_after_refresh(body.scroll_visible, animate=False)

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
