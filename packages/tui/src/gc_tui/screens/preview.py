"""PreviewScreen — read-only превью сессии реальными виджетами трейса.

Esc → dismiss(None) (назад). Enter → продолжить сессию (§3.3, путь 3):
модель экрана сессии строится в фоне из уже прочитанного превью, но со
входами целевой сессии; превью ждёт с индикатором на месте строки
подсказок (Esc отменяет и оставляет в превью), готово — замок и
dismiss(готовая история) — переход делает список.
Лента — как живая (`TraceFollowMixin`): открывается у низа, «не внизу» —
липкая реплика и плашка «в конец» (клик или End — к низу). Окно ленты то
же, что в сессии (`TraceWindowMixin`): смонтирован хвост, старое
домонтируется при прокрутке вверх, по возвращении вниз окно ужимается.
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
from gc_tui.models import TaskCursor
from gc_tui.screens.main import trace_screen_of
from gc_tui.screens.trace_follow import TraceFollowMixin, trace_overlays
from gc_tui.screens.trace_window import TraceWindowMixin
from gc_tui.services.clipboard_ops import copy_out
from gc_tui.services.history_loader import (
    HistoryLoader,
    LoadedHistory,
    history_inputs_for,
)
from gc_tui.services.history_model import HistoryModel
from gc_tui.services.image_open import open_image
from gc_tui.services.skill_view import truncate_path_left
from gc_tui.services.trace_retention import subagent_parent_ids_in_window
from gc_tui.widgets.open_indicator import OpenIndicator

_PATH_MAXLEN = 48
_FOOTER_HINTS = (
    "Enter — продолжить сессию · ↑/↓ / PgUp/PgDn — прокрутка · "
    "End — в конец · Esc — назад"
)


class PreviewScreen(
    TraceWindowMixin, TraceFollowMixin, Screen[LoadedHistory | None]
):
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
        cursor: TaskCursor | None = None,
        model: HistoryModel | None = None,
    ) -> None:
        super().__init__()
        # Слоты уровней действующих настроек — точки знака черт смены
        # модели.
        self._model_slots = model_slots
        # Прочитанное превью (сегменты и курсор сразу за ними) — для
        # перехода в сессию без повторного чтения (§5.7, путь 3).
        self._segments = segments
        self._cursor = cursor
        self._session_id = thread_id
        self._project_path = project_path
        self._subagent_records = subagent_records
        # Снимок MCP треда (meta.runtime.mcp_tools) — подписи MCP-вызовов.
        self._mcp_snapshot = mcp_snapshot
        # Блоки вызовов превью (P4): исполнители уровней и маршрут тогла.
        self._blocks = None
        # Окно ленты: модель истории (готовая из списка или своя по
        # сегментам), параметры отрисовки и вложенное.
        self._model = model
        self._chunk_options = None
        self._nested = None
        # Переход в сессию: загрузчик и индикатор в строке подсказок.
        self._loader: HistoryLoader | None = None
        self._indicator: OpenIndicator | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static(
                    f"превью · сессия {self._session_id[:8]}",
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

        from gc_tui.services.history_model import (
            HistoryInputs,
            build_history,
        )
        from gc_tui.services.history_render import ChunkOptions
        from gc_tui.services.nested_trace import NestedSource

        segments = self._segments
        records = self._subagent_records or []
        # корень из шапки превью: пути в подписях вызовов — от него
        root = self._project_path or None
        # Хранилища загрузчику не нужно: переход в сессию строит модель
        # из прочитанного превью.
        self._loader = HistoryLoader(None, self.app.call_later)
        injected = self._injected_fields()
        if self._model is None:
            self._model = build_history(
                segments,
                inputs=HistoryInputs(
                    root=root, injected_fields=tuple(injected)
                ),
            )
        blocks = self._blocks = self._preview_blocks()
        window_parents = subagent_parent_ids_in_window(segments)
        self._nested = NestedSource(
            lambda: records,
            window_parent_ids=lambda: window_parents,
            root=root,
            injected=injected,
        )
        self._chunk_options = ChunkOptions(
            focusable_compaction=False,
            edits_lookup=blocks.edits_lookup,
            diff_preview=blocks.diff_preview,
            expanded_turns=blocks.expanded_turns,
            mcp_tools=mcp_tools_from_snapshot(self._mcp_snapshot),
            mcp_servers=mcp_servers_of(self._mcp_snapshot),
            model_slots=self._model_slots or {},
            timers=blocks.timers,
            # В превью нет итога «сейчас»: строка последнего хода — как
            # все, и при домонтировании после снятия.
            fresh_line=False,
        )
        refs = self._window_mount_tail()
        blocks.views = refs.block_views
        # Трасса субагентов остаётся у узла вложенного — для кусков
        # сверху; сегменты ждут перехода в сессию.
        self._subagent_records = []
        # Как живая лента после открытия сессии — у низа.
        self._watch_trace_follow()
        self._scroll_trace_end()

    # ---- окно ленты ------------------------------------------------------

    def _window_feed(self):
        return self.query_one("#trace", VerticalScroll)

    def _window_model(self):
        return self._model

    def _window_options(self):
        return self._chunk_options

    def _window_nested(self):
        return self._nested

    def _window_adopt_chunk(self, refs) -> None:
        """Виды блоков куска сверху — в маршрут раскрытия превью."""
        self._blocks.views.update(refs.block_views)

    def _window_pinned(self, turn: int) -> bool:
        """Ход со строкой в фокусе не снимается (§5.3)."""
        focused = self.focused
        return focused is not None and bool(
            set(focused.ancestors_with_self)
            & set(self._window_turn_widgets(turn))
        )

    def _window_forget_turn(self, turn: int) -> bool:
        """Снятый ход уходит и из маршрута раскрытия: уровень хода и
        уровни субагентов (ключ — call_id строки `Agent`)."""
        from gc_tui.services.history_render import history_level

        removed = self._window_turn_widgets(turn)
        if self._window_pinned(turn):
            return False
        if not super()._window_forget_turn(turn):
            return False
        views = self._blocks.views
        views.pop(history_level(turn), None)
        for widget in removed:
            views.pop(getattr(widget, "call_id", None), None)
        return True

    def on_resize(self, _event) -> None:
        self.call_after_refresh(self._refresh_trace_overlays)
        self.call_after_refresh(self._window_fill)

    def _injected_fields(self) -> list[str]:
        """Служебные поля схем тулов (`agent.inject_tool_fields`); хост
        без настроек — нет."""
        settings = getattr(self.app, "base_settings", None)
        if settings is None:
            return []
        return list(settings().settings.agent.inject_tool_fields)

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
        if threads_dir and system_id and self._session_id:
            lookup = lookup_for_settings(
                threads_dir, system_id, self._session_id, cfg
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
        """Enter: идёт ход — вопрос о стопе поверх превью, затем загрузка;
        повторный Enter во время загрузки и хост без экрана сессии —
        ничего."""
        if self._loader.busy:
            return
        main = trace_screen_of(self.app)
        if main is None:
            return
        main.confirm_switch(self._session_id, lambda: self._open_session(main))

    def _open_session(self, main) -> None:
        tid = self._session_id
        footer = self.query_one("#preview-footer", Static)
        self._indicator = OpenIndicator(
            "открываю сессию",
            host=self,
            paint=lambda frame: footer.update(
                _FOOTER_HINTS if frame is None else frame
            ),
        )
        self._loader.start(
            tid,
            history_inputs_for(self.app, tid),
            segments=self._segments,
            cursor=self._cursor,
            withdrawn=main.withdrawn_in(tid),
            on_ready=lambda loaded: self._session_ready(main, loaded),
            on_error=self._open_failed,
        )

    def _open_failed(self, exc: Exception) -> None:
        """Сбой загрузки (битый файл треда): индикатор снят, превью
        остаётся."""
        self._stop_indicator()
        self.app.notify(f"Не удалось открыть: {exc}", severity="error")

    def _session_ready(self, main, loaded: LoadedHistory) -> None:
        """Сессия готова: замок до закрытия превью; занята — превью
        остаётся («занято» сказано)."""
        self._stop_indicator()
        if main.claim_session(self._session_id):
            self.dismiss(loaded)

    def _stop_indicator(self) -> None:
        if self._indicator is not None:
            self._indicator.stop()
            self._indicator = None

    def action_to_end(self) -> None:
        self._scroll_to_bottom()

    def action_back(self) -> None:
        """Esc: идёт открытие — отменить и остаться; иначе назад."""
        if self._loader.busy:
            self._loader.cancel()
            self._stop_indicator()
            return
        self.dismiss(None)

    def on_unmount(self) -> None:
        if self._loader is not None:
            self._loader.cancel()

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
