"""PreviewScreen — read-only превью сессии реальными виджетами трейса.

Esc → dismiss(False) (назад). Enter → dismiss(True) (продолжить сессию).
Лента — как живая (`TraceFollowMixin`): открывается у низа, «не внизу» —
липкая реплика и плашка «в конец» (клик или End — к низу).
Шапка: слева «превью · сессия <id>», справа — путь проекта. Футер — подсказки клавиш.
Промпт сессии виден в самом трейсе ниже, поэтому в шапку не дублируется."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from gc_tui import textual_compat
from gc_tui.messages import BlockToggle, CopyBlockRequest, OpenImageRequest
from gc_tui.screens.trace_follow import TraceFollowMixin, trace_overlays
from gc_tui.services.clipboard_ops import copy_out
from gc_tui.services.image_open import open_image
from gc_tui.services.skill_view import truncate_path_left
from gc_tui.services.trace_retention import subagent_parent_ids_in_window

_PATH_MAXLEN = 48
_FOOTER_HINTS = (
    "Enter — продолжить сессию · ↑/↓ / PgUp/PgDn — прокрутка · "
    "End — в конец · Esc — назад"
)


class PreviewScreen(TraceFollowMixin, Screen[bool]):
    # priority=True: Enter/Esc срабатывают ДО фокусной строки. Иначе focusable
    # ToolCallRow/TraceRow перехватывают Enter (open-detail + stop) и тред не
    # открывается. В превью нет ввода — Enter всегда = открыть тред, Esc = назад.
    BINDINGS = [
        Binding("enter", "resume", "продолжить сессию", priority=True),
        Binding("escape", "back", "назад", priority=True),
        Binding("end", "to_end", "в конец", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        segments: list,
        thread_id: str = "",
        project_path: str = "",
        subagent_records: list | None = None,
        mcp_snapshot: dict | None = None,
        model_slots: dict | None = None,
    ) -> None:
        super().__init__()
        # Слоты уровней действующих настроек — точки знака черт смены
        # модели.
        self._model_slots = model_slots
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
            trace = textual_compat.AnchoredScroll(id="trace")
            yield trace
            yield Static(_FOOTER_HINTS, id="preview-footer", markup=False)
        yield from trace_overlays(trace)

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
        calls = render_segments_readonly(
            trace,
            segments,
            model_slots=self._model_slots,
            show_thoughts=self._show_thoughts(),
            **opts,
        )
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
        # Как живая лента после открытия сессии — у низа.
        self._watch_trace_follow()
        self._scroll_trace_end()

    def on_resize(self, _event) -> None:
        self.call_after_refresh(self._refresh_trace_overlays)

    def _show_thoughts(self) -> bool:
        """Мысли модели — по действующему `tui.show_reasoning` окна; хост
        без настроек — выключено, как по умолчанию."""
        settings = getattr(self.app, "effective_settings", None)
        if settings is None:
            return False
        return settings().settings.tui.show_reasoning == "on"

    def _preview_blocks(self):
        """Блоки превью с журналом правок треда (только чтение) и правилом
        превью диффов из `agent_edits` — `edit_settings` (спека P4 §6.3,
        §9.10а): правки — как вживую."""
        from gc_tui.services.block_edits import edit_settings
        from gc_tui.services.edits_lookup import lookup_for_settings
        from gc_tui.widgets.tool_blocks_view import PreviewBlocks

        settings = getattr(self.app, "base_settings", None)
        if settings is None:
            # Хост без настроек и тредов (превью вне приложения) — лимиты
            # по умолчанию, без журнала правок.
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
        блок своего уровня (Enter остаётся «открыть тред»); прокрутка —
        как в живой ленте (§9.3)."""
        message.stop()
        if self._blocks is not None:
            self._toggle_trace_block(self._blocks.views, message)

    def action_resume(self) -> None:
        self.dismiss(True)

    def action_to_end(self) -> None:
        self._scroll_to_bottom()

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
