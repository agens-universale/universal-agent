"""TraceScreen — главный экран: трейс событий, инпут с палитрой команд и
пикерами (skill/arg/file), ask_human-баннер, pending-реплики и polling треда."""
from __future__ import annotations

import os
import threading
import time
import uuid
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Static, TextArea

from intent_log.record import ACTION_USER_ASK
from intent_log.serialize import deserialize_list
from runtime_notices import collect_unread_runtime_notices
from tui.commands import build_default_registry, closest_command
from tui.messages import CopyBlockRequest
from tui.models import TaskCursor
from rich.cells import cell_len
from rich.text import Text
from tui.palette import ACCENT as _ACCENT
from tui.palette import DEEP_DIM as _DEEP_DIM
from tui.palette import DIM as _DIM
from tui.palette import ERR as _ERR_RED
from tui.palette import INK as _INK
from tui.services.context_meter import render_context_meter
from tui.services.notices_ui import (
    footer_left_rows,
    footer_left_with_badge,
    format_notices_badge,
)
from tui.services.usage_meter import compose_adaptive_footer_right, usage_segments
from tui.services.clipboard_ops import copy_out
from tui.services.export import export_thread
from tui.services.path_display import truncate_path
from tui.services.git_info import current_branch
from tui.services.session_lock import BUSY_MESSAGE, heartbeat, release, try_acquire
from tui.services.stores.file_store import FileTaskStore
from tui.services.subagent_progress import (
    SubagentProgressConsumer,
    format_parent_live_status,
    reconcile_batch_phases,
)
from tui.services.subagent_sidecar_mapper import (
    CHILD_SUMMARY_LABEL,
    apply_sidecar_nested_actions,
    plan_sidecar_nested_actions,
    resolve_nested_tool_detail,
    trace_events_from_nested_detail,
)
from tui.services.trace_mapper import collect_skipped_call_ids, map_message
from tui.services.trace_render import (
    LINE_INTERMEDIATE_CLASS,
    LINE_USER_CLASS,
    NESTED_PREFIX,
    NESTED_PROSE_PREFIX,
    NESTED_USER_PREFIX,
    build_compaction_label,
)
from tui.screens.tool_call_detail import ToolCallDetailScreen
from tui.session_settings import read_breaker_from_meta, read_from_meta
from tui.toggles import (
    PROFILE_NONE_VALUE,
    TOGGLES,
    apply_toggle,
    dynamic_toggle_hint,
    next_toggle_value,
    preset_custom_hint,
    preset_track_extra,
    toggle_state,
)
from tui.widgets.command_input import CommandTextArea
from tui.widgets.command_palette import CommandPalette
from tui.widgets.thinking_indicator import (
    TERMINAL_INDICATOR_STATES,
    ThinkingIndicator,
    is_terminal_indicator_state,
)
from tui.widgets.tool_call_row import ToolCallRow
from tui.widgets.trace_row import TraceRow
from tui import branding
from tui.services.terminal_title import format_terminal_title, set_terminal_title
from tui.services.session_title import (
    LAST_ACTUAL_MIN,
    display_name,
    normalize_prompt_title,
)
from tui.services.reasoning_clock import COGNITIVE_TOOLS, ReasoningClock
from tui.services.voice_pill import render_pill


def resolve_terminal_outcome(*, runner_error, stop_requested, terminal_outcome):
    """Выбор терминального исхода по флагам прогона. Порядок важен:
    error → stop → лимитные исходы → completed (дефолт)."""
    if runner_error:
        return "failed"
    if stop_requested:
        return "stopped"
    if terminal_outcome == "step_limit":
        return "stopped_steps"
    if terminal_outcome == "tool_errors":
        return "stopped_errors"
    if terminal_outcome == "token_limit":
        return "stopped_tokens"
    if terminal_outcome == "tool_call_limit":
        return "stopped_tool_calls"
    return "completed"


def resolve_live_terminal_indicator_state(*, last_indicator_kind, status, stop_requested):
    """Состояние терминального индикатора в idle-фазе живого прогона.
    last_indicator.kind несёт точный лимитный исход (stopped_steps/stopped_errors),
    тогда как meta.tui.status схлопнут в Status-литерал ("stopped"). Для лимитных
    исходов предпочитаем kind; для прочих — прежняя логика status→state с guard
    прямого stop в обход _cmd_stop."""
    if last_indicator_kind in ("stopped_steps", "stopped_errors", "stopped_tokens", "stopped_tool_calls"):
        return last_indicator_kind
    outcome = status
    if outcome == "completed" and stop_requested:
        outcome = "stopped"
    return {"completed": "done", "stopped": "cancelled", "failed": "failed"}.get(outcome)


def select_indicator_mode(*, err, is_running, ask_human, confirm):
    """Режим строки-индикатора в _poll_active. Порядок важен.

    Human-wait (ask_human/confirm) выше err: агент ждёт ответ юзера — баннер
    уже говорит об этом; ✗ Ошибка поверх «АГЕНТ СПРАШИВАЕТ» путает (в т.ч. когда
    stale last_runner_error от заменённого runner ещё не сброшен).

    Иначе err (краш текущего раннера) → "failed" сразу, не дожидаясь reaping /
    falling-edge: при быстром следующем сообщении статус иначе теряется, а
    idle-ветка уничтожает индикатор."""
    if ask_human or confirm:
        return "hide"
    if err:
        return "failed"
    if is_running:
        return "active"
    return "terminal"


def select_active_substate(
    *,
    summarizing,
    finalizing,
    last_event_kind,
    since_last,
    soft_hide_kinds,
    finish_gate_checking=False,
):
    """Под-состояние индикатора в режиме "active". Порядок = приоритет.

    - summarizing (компакция истории) → "compressing" (сжимаю контекст…);
    - finish_gate_checking (судья turn_finish реально сейчас вызывается) →
      "processing" (сверяюсь…) — ВЫШЕ защёлки _finalized/finalizing, иначе
      «завершаю» перебивает и судья работает невидимо;
    - finalizing (принят финальный ответ — turn_finish ИЛИ финальная проза —
      идёт запись/саммари/flush/cleanup) → "validating" (завершаю…);
    - последнее событие скрыто (agent/system) и висит ≥1.5с (finish-gate сверяет
      ответ / bounce / доделка) → "processing" (сверяюсь…);
    - иначе → "thinking" (думаю…).
    """
    if summarizing:
        return "compressing"
    if finish_gate_checking:
        return "processing"
    if finalizing or last_event_kind == "_finalized":
        return "validating"
    if last_event_kind in soft_hide_kinds and since_last >= 1.5:
        return "processing"
    return "thinking"


# Футер с функциональными клавишами; /stop добавляется только при активном run.
# (/skills, /export доступны через палитру — в футере лишние. /mcp не дублируем —
# подсказка «/mcp» уже в бренд-карточке.)
# Diff: F3 в базовом хроме футера; CTA в трейсе — пути pending + F3/diff.
# Отступ колонки статусов от логотипа — тот же, что в CSS (#brand-mcp padding-left):
# порог узости считается по нему, и разъехаться они не должны.
_MCP_COL_PAD = 6
# F7/buffer пришёл из develop (синк 2026-08-06) — клавиша живёт в хроме наравне с
# остальными.
_FOOTER_LEFT = "F1/help  F2/sessions  F3/diff  F5/config  F6/settings  F7/buffer"
# Во время хода слева остаётся ТОЛЬКО остановка (Task 17, решение юзера):
# F-подсказки не показываются никогда, независимо от ширины терминала —
# пользователь их уже знает, а место во время хода нужно правой колонке, там
# живут стрелка перехода и сегмент «запрос». Это безусловное правило, а не
# ступень деградации по ширине.
_FOOTER_RUNNING_LEFT = "Ctrl+C/stop"
_PLACEHOLDER_READY = "опишите задачу или /команда"
_HINT_TEXT = {
    "clear": "Esc ещё раз, чтобы очистить",
    "stop":  "Esc ещё раз, чтобы остановить",
}
_PASTE_HINT_TEXT = "вставьте ещё раз, чтобы развернуть"
# Модель и backend-режим показываются в шапке экрана (_compose_header_segments).

# Лесенка усечения имени профиля в футере (Task 9, docs
# 2026-08-09-profiles-a-migration-plan): потолок пробуется ВСЕГДА, даже когда
# места хватает с избытком — длинное имя не должно отжирать футер; дальше
# ужимается по факту нехватки ширины вплоть до минимума, ниже которого имя
# снимается целиком.
_PROFILE_NAME_MAX_COLS = 14   # потолок: 13 символов + «…»
_PROFILE_NAME_MIN_COLS = 5    # минимум: 4 символа + «…» (Task 17)

# Хром футера: padding #footer (0 1 → 2) + padding-right #footer-right (1) +
# зазор между колонками, который держит padding-right у #footer-left (2). На
# столько ширина всего футера больше суммы контента обеих колонок.
_FOOTER_CHROME_COLS = 5
# Раскладка usage-блока внутри варианта правой колонки: «всё одной строкой» и
# «цены вниз» — не результат подбора ширины, а РАЗНЫЕ ступени лесенки, поэтому
# `compose_adaptive_footer_right` получает не бюджет, а заведомый предел,
# включающий нужную ветку.
_RIGHT_ONE_LINE = 10 ** 6
_RIGHT_PRICES_DOWN = 0
# «Ещё не резолвили» для необязательных аргументов, у которых None — законное
# значение (плашка контекста скрыта).
_UNRESOLVED = object()

# Снимок футер-головы (Task 15, docs 2026-08-09-profiles-a-migration-plan):
# profile/level_color/model — «факт» (пин хода или, вне хода, живой резолв);
# transition — (цвет_нового_уровня_или_None, новая_модель) или None (переход
# не нужен). См. `TraceScreen._footer_head_snapshot`.
_FooterHeadSnapshot = namedtuple(
    "_FooterHeadSnapshot", ("profile", "level_color", "model", "transition")
)


def _truncate_profile_name(name: str, max_cols: int) -> str:
    """Режет ХВОСТ имени профиля до `max_cols` колонок (cell-width), с «…» на
    конце, если целиком не влезает."""
    if cell_len(name) <= max_cols:
        return name
    out = ""
    for ch in name:
        if cell_len(out + ch) > max_cols - 1:
            break
        out += ch
    return out + "…"


class TraceScreen(Screen):
    """Главный экран: трейс, ввод пользователя, команды и сопровождение run'а."""

    # Ctrl+A — select-all в #cmdinput (CommandTextArea); approve-all только на /diff.
    # Ctrl+V: priority — иначе при фокусе в трейсе (клик по строке во время run)
    # хоткей никуда не доходит: у TraceRow/ToolCallRow нет action_paste.
    BINDINGS = [
        Binding("ctrl+v", "paste_to_input", "вставить", show=False, priority=True),
        Binding("ctrl+м", "paste_to_input", "вставить", show=False, priority=True),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._registry = build_default_registry()
        self._palette: CommandPalette | None = None
        self._brand: Vertical | None = None
        self._brand_wide: bool | None = None   # раскладка карточки: статусы сбоку/снизу
        self._brand_row = None                 # контейнер карточки (ссылка, не запрос)
        self._task_events: list = []  # list[TraceEvent]; пополняется в _render_record/_render_segments
        self._last_agent_summary: Optional[str] = None  # A1: дедуп равных user-facing рядов
        self._store: Optional[FileTaskStore] = None
        self._cursor: Optional[TaskCursor] = None
        self._next_no: int = 1
        self._active_task_id: Optional[str] = None
        self._session_lock_hb_mono: float = 0.0  # throttle heartbeat ≤ 1 Гц
        self._error_shown: bool = False
        self._mcp_status_row = None   # Optional[Static] — live MCP-loading индикатор
        self._warnings_toasted = False  # warning-тосты при старте показываются один раз за запуск
        self._mcp_status_done = False
        # последняя MCP-строка карточки — для перерисовки (runtime-notice / update-notice)
        self._last_mcp_card_line: Optional[str] = None
        # Phase 2: tool-row mutation state
        self._active_calls: dict[str, ToolCallRow] = {}
        # Nested progress под subagent_*: live sink + reopen из data_subagents_*.
        self._subagent_progress = SubagentProgressConsumer()
        self._nested_tool_rows: dict[str, ToolCallRow] = {}  # child tool_call_id → row
        self._nested_widgets_by_parent: dict[str, list] = {}  # parent_call_id → widgets
        self._nested_anchor: dict[str, ToolCallRow] = {}  # parent_call_id → last mounted nested
        # Durable parent rows for subagent_* (survive pop from _active_calls on result).
        self._subagent_parent_rows: dict[str, ToolCallRow] = {}
        self._reasoning_clock = ReasoningClock()
        self._submit_solution_call_ids: set[str] = set()
        # Track submit_solution agent-line widgets, чтобы при повторе свернуть предыдущие.
        # list of (event_no, main_static, note_widget_or_None)
        # spec 2026-05-26 (code-block-highlight): теперь храним СПИСОК виджетов
        # предыдущей submit_solution (multi-widget рендер: prose + code-panels).
        self._submit_solution_widgets: list[tuple[list[Widget], Optional["TraceRow"]]] = []
        # Mapping event_no → summary-body TraceRow (скрыт; раскрывается при фокусе маркера).
        self._summary_body_widgets: dict[int, "TraceRow"] = {}
        self._summary_marker_widgets: dict[int, "TraceRow"] = {}
        # Phase 5: ask_human banner state
        self._ask_human_banner = None  # type: Optional[AskHumanBanner]
        self._ask_human_call_id: Optional[str] = None
        # confirm banner state (T2)
        self._confirm_banner = None   # type: Optional["ConfirmBanner"]
        self._confirm_active: bool = False
        # Скрытая трейс-строка вопроса; раскрывается при схлопывании баннера.
        self._ask_human_question_row = None  # type: Optional[TraceRow]
        # Раннер, задавший текущий ask_human (владелец вопроса). None, если плашка
        # воспроизведена из истории — тогда ответ идёт новым ходом, а не в stdin.
        self._ask_human_owner_runner = None
        # Child HITL: parent_call_id субагента, пока висит его ask_human-баннер.
        # Ответ → stdin child, без parent pending / user-строки в data_*.
        self._ask_human_child_parent_call_id: Optional[str] = None
        # Spec 2026-05-26: pending user-replies (in-memory dict by client_id, FIFO ordered).
        self._pending_user_widgets: dict[str, "UserReplyContainer"] = {}
        # Кэш wire_content для reconciliation — независим от состояния meta-файла,
        # который executor может перезаписывать в своём потоке.
        self._pending_wire_cache: dict[str, str] = {}
        self._skill_picker = None  # type: Optional["SkillPicker"]
        self._arg_picker = None  # type: Optional["ArgPicker"]
        self._file_picker = None  # FilePicker | None
        self._skills_cache: Optional[list[dict]] = None
        # После Tab-completion в skill picker гасим один Changed-цикл, чтобы
        # on_text_area_changed не переоткрыл только что закрытый picker.
        self._suppress_picker_reopen: bool = False
        # Spec 2026-05-26: ThinkingIndicator state.
        self._thinking = None  # type: Optional["ThinkingIndicator"]
        self._trace_max_height = None  # type: Optional[int]
        self._stick_bottom = True   # автоскролл-намерение «липнуть к низу» трейса
        self._last_auto_y = 0       # куда мы в прошлый раз программно прижали трейс
        # Freeze UI на время app-select (§3.2 F1): time.monotonic() первого тика,
        # где увидели Screen._selecting; None когда не selecting.
        self._select_freeze_since: Optional[float] = None
        # Эфемерный StreamBlock — live-вывод текущего chat_completions-шага;
        # снимается, когда приходит каноничная assistant-строка / run завершён.
        self._stream_block = None  # type: Optional["StreamBlock"]
        self._stream_answer_row = None  # type: Optional["TraceRow"]
        # Кумулятивный таймер «думаю…» — стартует на каждом user-send,
        # передаётся в виджет, чтобы elapsed не сбрасывался при show/hide циклах.
        self._thinking_started_at: Optional[float] = None
        # Эвристика «скрыть после видимого ответа агента»: после event kind=agent
        # с паузой ≥1.5с прячем индикатор (intent_guard / policy ещё работают,
        # но юзер уже видит ответ). Следующий event снимет .hidden.
        import time as _time
        self._last_event_at: float = _time.monotonic()
        self._last_event_kind: Optional[str] = None
        # Cancel-with-undo: счётчик не-user trace-событий с момента последнего user-send.
        # Если == 0 при Esc-stop → агент ещё ничего не сгенерировал → rollback.
        self._events_since_last_user_send: int = 0
        self._last_user_send_text: str = ""
        # Bang-shell (`!cmd`): один concurrent job; эфемерно в трейсе, не в thread.
        self._bang = None  # type: Optional["BangShellController"]
        self._bang_timeout_sec: Optional[float] = None
        # Блоки bang в трейсе (header + result) для `! clear` / `! clear N`.
        self._bang_blocks: list[list] = []
        self._bang_open_block: Optional[list] = None
        # Заголовок терминала: кэш промпта возобновлённой сессии (читается из стора
        # 1 раз на attach, не на каждый poll-тик) + дедуп последнего записанного
        # заголовка (poll зовёт обновление каждый тик — пишем OSC только при смене).
        # Промпт заголовка терминала — ЕДИНЫЙ источник: последнее user-сообщение
        # активной сессии. Ставится из стора на attach и = тексту на каждом send.
        # (Раньше второй источник _last_user_send_text давал прилипание/устаревание.)
        self._resumed_prompt: Optional[str] = None
        self._last_terminal_title: Optional[str] = None
        # client_id последней pending-реплики — для полного отката в cancel-with-undo.
        self._last_pending_client_id: Optional[str] = None
        # id последнего user-сообщения (пробрасывается в run) — чтобы точечно
        # удалить осиротевшую реплику из треда при cancel-with-undo.
        self._last_user_message_id: Optional[str] = None
        # Заявки на отложенное удаление осиротевших реплик: список
        # (runner, task_id, message_id). Исполняются в poll, когда тред idle.
        self._pending_orphan_cleanups: list[tuple] = []
        # Флаг состояния runner на предыдущем тике — для детекта спадающего фронта.
        self._was_running: bool = False
        self._was_summarizing: bool = False   # для baseline last_compact_msg_count
        # Post-turn CTA «Изменено N · /diff». Причина взвода — ровно два случая,
        # правило B (не показывать после хода без событий) применимо только к
        # первому: "turn_end" — обычный итог хода; "wipe_restore" — wipe трейса
        # стёр уже смонтированную CTA, её возвращают независимо от итога хода.
        self._diff_pending_cta_pending: str | None = None
        # Транзиентная подсказка в footer-left (esc-confirm и paste-hint делят слот).
        self._esc_confirm_kind: Optional[str] = None  # "clear" | "stop" | None
        self._footer_hint_timer = None                # textual.Timer
        self._footer_hint_active: bool = False
        # Input history: per-project ↑/↓ навигация.
        # _history создаётся в on_mount (project_dir = threads_dir.parent).
        self._history = None  # type: Optional["InputHistory"]
        self._history_index: Optional[int] = None
        self._history_draft: Optional[str] = None
        # Защита от self-reset: history-навигация программно меняет input.text,
        # что триггерит on_text_area_changed → reset_history_navigation.
        # Храним последний программно выставленный текст; Changed, у которого
        # текущее значение совпадает с ним, — не user-edit, не сбрасываем state.
        self._history_last_programmatic_text: Optional[str] = None
        # Отложенный маркер «[skill] X загружен/не найден»: ставится в
        # _consume_pending_prefix, монтируется после реплики (_mount_user_reply).
        self._pending_skill_marker: Optional[tuple[str, bool]] = None

        # Голосовой ввод (спека docs/tui/specs/2026-07-05-voice-input-design.md):
        # flow собирается лениво на первый Ctrl+T; таймер листает кадры пилюли.
        self._voice = None            # type: Optional["VoiceInputFlow"]
        self._voice_timer = None      # textual.Timer | None
        self._voice_frame: int = 0
        # Конфиг перечитали посреди записи: рвать её нельзя, а кэш flow уже протух —
        # снимем его, когда запись кончится (см. config_reloaded / _on_voice_state).
        self._voice_flow_stale: bool = False
        # Идёт проверка живости батарейки или её подъём (воркеры Ctrl+T): второй такой
        # же по повторному нажатию не заводим.
        self._voice_service_pending: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            left_text, right_text = self._compose_header_segments()
            with Horizontal(id="header"):
                yield Static(left_text, id="header-left", markup=False)
                yield Static(right_text, id="header-right", markup=False)
            yield VerticalScroll(id="trace")
            yield Vertical(id="indicator-bar")
            with Horizontal(id="input-row"):
                yield Static("> ", id="input-prompt", markup=False)
                yield CommandTextArea(placeholder=_PLACEHOLDER_READY, id="cmdinput")
            with Horizontal(id="footer"):
                yield Static(_FOOTER_LEFT, id="footer-left", markup=False)
                yield Static(self._compose_footer_right(), id="footer-right", markup=False)

    def _compose_header_segments(self) -> "tuple[str, str]":
        """Return (left_text, right_text) for header.

        Свежий экран (нет активной задачи):
          left = «Добро пожаловать в GigaCode»
          right = адаптивный cwd

        Активная задача:
          left = «Сессия: <8ch>»
          right = адаптивный cwd
        """
        if self._active_task_id is None:
            left = branding.WELCOME
        else:
            left = f"Сессия: {self._active_task_id[:8]}"
        right = truncate_path(self._current_cwd(), self._header_right_cols())
        return left, right

    def _header_right_cols(self) -> int:
        """Доступная ширина правого слота хедера для усечения пути."""
        try:
            w = self.app.size.width
        except Exception:
            w = 80
        return max(12, w - 34)

    def on_resize(self, event) -> None:
        self._refresh_header()   # пересчитать усечение пути под новую ширину
        self._refresh_footer()   # адаптивная вёрстка usage (стек сессии) под новую ширину
        self._sync_trace_max_height()   # потолок #trace под новую высоту экрана
        # Раскладка бренд-карточки выбиралась только при сборке: сузив окно уже после
        # старта, человек получал колонку статусов в пару символов шириной — слова
        # рвались по буквам («подк / люче / ны»), а совсем узко она схлопывалась в ноль.
        self._apply_brand_layout(self._brand_fits_beside(event.size.width))

    def on_click(self, event: events.Click) -> None:
        """Клик в ЛЮБОЙ части трейса → фокус на #trace, чтобы колесо/стрелки/PgUp-
        PgDn скроллили его (а не зависели от попадания в пустой край). Сюда доходит
        только клик по пустому месту / не-focusable строке: focusable строки сами
        останавливают событие в своём on_click (self.focus() + event.stop()).
        Клик вне трейса (инпут/футер) фокус трейса не трогает."""
        try:
            trace = self.query_one("#trace", VerticalScroll)
        except Exception:
            return
        if not trace.has_focus and trace.region.contains(event.screen_x, event.screen_y):
            trace.focus()

    def action_paste_to_input(self) -> None:
        """Ctrl+V → всегда в #cmdinput, даже если фокус ушёл в трейс (клик/Tab
        во время run). Иначе хоткей «мёртв»: у строк трейса нет action_paste."""
        try:
            inp = self.query_one("#cmdinput", CommandTextArea)
        except Exception:
            return
        if inp.disabled:
            return
        if not inp.has_focus:
            inp.focus()
        inp.action_paste()

    async def _on_paste(self, event: events.Paste) -> None:
        """Bracketed-paste при фокусе не в инпуте (трейс) → вставить в #cmdinput.

        Иначе events.Paste уходит в TraceRow/VerticalScroll и пропадает."""
        focused = self.focused
        if isinstance(focused, CommandTextArea):
            return  # CommandTextArea._on_paste сам разберёт
        try:
            inp = self.query_one("#cmdinput", CommandTextArea)
        except Exception:
            return
        if inp.disabled or inp.read_only:
            return
        event.stop()
        event.prevent_default()
        if not inp.has_focus:
            inp.focus()
        inp._handle_paste(event.text)

    def _footer_model_name(self) -> str:
        """Действующая модель СЛЕДУЮЩЕГО хода из Router (спека §12.2), не
        плоский ключ `cfg["model"]`: должна отражать активный уровень/профиль
        сразу после /level или /profiles, до пересборки executor (Router
        витрин строится от `effective_config()` на каждый вызов, §6.4).

        Легаси-плейсхолдер шаблона (Task 14) — не имя модели, показывать нечего."""
        from onboarding import MODEL_SENTINEL
        display_router = getattr(self.app, "display_router", None)
        if not callable(display_router):
            cfg = getattr(self.app, "_config", None) or {}
            model = cfg.get("model")
            return model if model and model != MODEL_SENTINEL else "?"
        # check_breaker=False: витрина показывает модель честно даже при открытом
        # активном алиасе (спека §12.2) — иначе футер молча замирал бы (§8).
        resolved = display_router().resolve("main", check_breaker=False)
        model = resolved.alias or resolved.model
        return model if model and model != MODEL_SENTINEL else "?"

    def _footer_live_level(self) -> str:
        """Активный уровень ЖИВОГО резолва (спека §12.1/§12.2): реагирует на
        /level и активацию профиля сразу, без пересборки executor. Общий
        источник для цвета точки будущего состояния и сравнения «уровень
        менялся?» посреди хода (спека Task 15)."""
        display_router = getattr(self.app, "display_router", None)
        return display_router().active_level() if callable(display_router) else "default"

    def _level_color(self, level: str) -> str:
        """Цвет точки-индикатора для конкретного уровня — из ЕДИНОГО источника
        `TOGGLES["level"]` (спека §12.1): не хардкод, будущий авто-режим со
        своим цветом подхватится сам."""
        colors = {v.value: v.color for v in TOGGLES["level"].values}
        return colors.get(level, _INK)

    def _footer_level_color(self) -> str:
        """Цвет точки-индикатора уровня ЖИВОГО резолва — та же Router-витрина,
        что у `_footer_model_name`."""
        return self._level_color(self._footer_live_level())

    def _footer_profile_name(self) -> "str | None":
        """Имя активного профиля для футера, или None — тот же источник, что
        `/config`/`/status` (`app.active_profile_ref`)."""
        ref_fn = getattr(self.app, "active_profile_ref", None)
        ref = ref_fn() if callable(ref_fn) else None
        return ref.name if ref is not None else None

    def _footer_pinned_resolution(self) -> "tuple[str, str, str] | None":
        """(уровень, цвет, имя модели) ХОДА, зафиксированного на пине (спека
        Task 15) — «факт», на чём реально идут запросы, а не живой резолв.

        None — активного хода нет (раннер отсутствует / не бежит), пина нет
        (легаси-старт не через `begin_turn`) или у раннера нет Router'а этого
        executor'а. Резолвит `pinned_level()` ЧЕРЕЗ `pinned_router()` — снимок
        `models`/`slots`/`assign`, зафиксированный при сборке ИМЕННО этого
        executor'а, — не живой `display_router()` TUI: тот отражает /profiles
        мгновенно и подменил бы «факт» на модель уже переключившегося профиля
        раньше, чем ход её реально увидит."""
        runner = getattr(self.app, "active_runner", None)
        is_running = getattr(runner, "is_running", None)
        if runner is None or not callable(is_running) or not is_running():
            return None
        pinned_level_fn = getattr(runner, "pinned_level", None)
        pinned_router_fn = getattr(runner, "pinned_router", None)
        if not callable(pinned_level_fn) or not callable(pinned_router_fn):
            return None
        level = pinned_level_fn()
        router = pinned_router_fn()
        if not level or router is None:
            return None
        from model_router import AliasCircuitOpenError
        from onboarding import MODEL_SENTINEL
        try:
            resolved = router.resolve_level(level)
        except AliasCircuitOpenError as exc:
            resolved = exc.resolved
        except ValueError:
            return None
        model = resolved.alias or resolved.model
        if not model or model == MODEL_SENTINEL:
            return None
        return (level, self._level_color(level), model)

    def _footer_head_snapshot(self) -> "_FooterHeadSnapshot":
        """Снимок футер-головы — один резолв на отрисовку футера.

        `profile`/`level_color`/`model` — «факт»: то, на чём реально идёт
        текущий ход (пин, спека Task 15), либо, вне хода, живой резолв, как
        раньше. `transition` — `(цвет_нового_уровня_или_None, новая_модель)`,
        что применится ПОСЛЕ хода, или `None` — переход не нужен (хода нет,
        будущая модель совпадает с фактической). Каждый резолв недёшев: имя
        профиля глобит оба корня профилей (диск), цвет/модель строят Router
        витрин от `effective_config()` — лесенка усечения в
        `_compose_footer_right` обязана получить снимок готовым, не резолвить
        заново на каждой ступени."""
        profile = self._footer_profile_name()
        live_level = self._footer_live_level()
        live_color = self._level_color(live_level)
        live_model = str(self._footer_model_name())
        pinned = self._footer_pinned_resolution()
        if pinned is None:
            return _FooterHeadSnapshot(profile, live_color, live_model, None)
        pinned_level, fact_color, fact_model = pinned
        if fact_model == live_model:
            return _FooterHeadSnapshot(profile, fact_color, fact_model, None)
        transition = (live_color if pinned_level != live_level else None, live_model)
        return _FooterHeadSnapshot(profile, fact_color, fact_model, transition)

    def _footer_head(self, *, with_profile: bool,
                      profile_name_cols: int = _PROFILE_NAME_MAX_COLS,
                      snapshot: "_FooterHeadSnapshot | None" = None,
                      with_transition: bool = True,
                      meter: "Text | None | object" = _UNRESOLVED) -> "Text":
        """head = [профиль ] ● модель[ → [● ]модель][ · контекст]. Точка —
        жирная, цвет уровня, сама и есть разделитель перед моделью (спека
        Task 8). Переход «сейчас → станет» (спека Task 15) — только посреди
        хода и только если будущая модель отличается от фактической; точка
        нового уровня — только если уровень менялся.

        Имя профиля режется ХВОСТОМ до `profile_name_cols` колонок (Task 9):
        потолок — `_PROFILE_NAME_MAX_COLS`, чтобы длинное имя не отжирало
        футер даже на широком экране.

        `snapshot` — готовый результат `_footer_head_snapshot()`; не передан —
        резолвим сами (одиночный вызов вне лесенки). `with_transition=False` —
        порядок жертв по ширине (спека Task 15): блок за стрелкой снимается
        ПОСЛЕ того, как имя профиля снято целиком — точка и факт-модель
        остаются всегда.

        `meter` — готовая плашка контекста (`None` = скрыта); не передана —
        резолвим сами. Как и `snapshot`, это ОДИН резолв на кадр: метр читает
        meta треда с диска, а лесенка строит голову до 22 раз."""
        if snapshot is None:
            snapshot = self._footer_head_snapshot()
        if meter is _UNRESOLVED:
            meter = self._context_meter()
        profile, level_color, model_name, transition = snapshot
        head = Text()
        if with_profile and profile:
            head.append(_truncate_profile_name(profile, profile_name_cols), style=_INK)
            head.append(" ")
        head.append("●", style=f"bold {level_color}")
        head.append(" ")
        head.append(model_name, style=_INK)
        if with_transition and transition is not None:
            future_color, future_model = transition
            head.append(" → ", style=_DIM)
            if future_color is not None:
                head.append("●", style=f"bold {future_color}")
                head.append(" ")
            head.append(future_model, style=_INK)
        if meter is not None:
            head.append(" · ", style=_DIM)
            head.append_text(meter)
        return head

    def _compose_footer_right(self) -> "Text":
        """Правый край футера: [профиль ]● модель[ → …] · контекст [+ цены вниз].

        Единая лесенка жертв (Task 17, порядок — дословное решение юзера);
        первое подошедшее и есть ответ:

        1. всё одной строкой справа, левая в одну строку, имя профиля в потолке;
        2. цены («запрос · сессия») уезжают на 2-ю строку справа, контекст
           остаётся наверху; левая ещё в одну строку, имя в потолке;
        3. левая переносит подсказки на свою 2-ю строку (по одной с хвоста —
           это делает Rich-wrap); имя профиля ВСЁ ЕЩЁ в потолке;
        4. имя профиля ужимается `_PROFILE_NAME_MAX_COLS` → `_MIN_COLS`;
        5. имя профиля снимается целиком;
        6. снимается блок за стрелкой — последняя жертва.

        Ступени 1-3 ничего не теряют: вторая строка футера (height: 2)
        зарезервирована в ОБЕИХ колонках и так, и перенос подсказок вниз — то же
        перекладывание в уже оплаченное место, что и перенос цен. Потеря
        информации начинается с 4-й. Отсюда и порядок (живой смок юзера,
        фикс-раунд 3): бесплатные ходы идут раньше платных, поэтому перебор
        имени живёт ЦЕЛИКОМ во втором проходе, а в первом пробуется только
        потолок — раньше имя ужималось при левой, спокойно стоявшей в один ряд.

        Лесенка МОНОТОННА по СОДЕРЖИМОМУ: при сужении окна цены, имя профиля и
        переход не возвращаются. Поэтому вариант «всё одной строкой» живёт лишь
        в первом проходе — иначе на границе ступени 3 (левой разрешили 2 ряда,
        места стало больше) цены прыгали бы обратно наверх, а это ровно то
        дёрганье, против которого задача и заведена. Уехали вниз — остались
        внизу. По той же причине во втором проходе перебор имени начинается с
        потолка: имя, ужатое в первом проходе, иначе «подросло» бы обратно.

        Число рядов ЛЕВОЙ монотонности не подчиняется, и запирать его не надо
        (решение по ревью): на ступенях 5-6 освободившаяся ширина может вернуть
        левую в один ряд. Это зеркало того же обмена «ряд слева ↔ сегмент
        справа», информации при этом не теряется, а искусственный запрет
        тратил бы место впустую.

        Ширину правая колонка не «получает», а ЗАЯВЛЯЕТ: #footer-right —
        width: auto, #footer-left — 1fr, остаток достаётся левой. Поэтому
        лесенка перебирает варианты правой с предикатом «сколько рядов при
        этом остатке занимает левая», а не считает бюджет от левой (так было
        до Task 17: левая не отдавала ни колонки никогда, и правая заранее
        теряла имя профиля и переход — живой смок).

        «Рядов левой» считает `footer_left_rows` — то же ядро, что её и
        рисует: параллельный оценщик разъехался бы с отрисовкой.

        `snapshot` резолвится ОДИН раз на кадр (perf, cfccade9) — лесенка
        получает его готовым и заново не резолвит."""
        request_seg, session_seg = self._usage_segments()
        available = self._footer_right_available_width()
        total = self._footer_total_width()
        snapshot = self._footer_head_snapshot()
        meter = self._context_meter()
        left_commands = self._footer_left_commands()
        left_badge = format_notices_badge(self._collect_footer_notices())

        def build(profile_cols: "int | None", layout_width: int,
                  *, with_transition: bool = True,
                  with_request: bool = True, with_prices: bool = True) -> "Text":
            head = self._footer_head(with_profile=profile_cols is not None,
                                      profile_name_cols=profile_cols or _PROFILE_NAME_MAX_COLS,
                                      snapshot=snapshot, with_transition=with_transition,
                                      meter=meter)
            request = request_seg if (with_prices and with_request) else None
            session = session_seg if with_prices else None
            if request is None and session is None:
                return head
            return compose_adaptive_footer_right(head, request, session, layout_width)

        def width_of(result: "Text") -> int:
            return max(cell_len(line) for line in result.plain.split("\n"))

        # Пол левой колонки. Обычный `_FOOTER_LEFT` — список F-подсказок: его не жаль
        # ни перенести, ни укоротить с хвоста. Но когда слева стоят КРИТИЧНЫЕ клавиши
        # («Ctrl+C/stop» во время хода, ответ на ask_human), урезать их нечем: другого
        # способа остановить агента или ответить ему нет. Их ширина резервируется, и
        # жертвует дальше правая колонка, а не левая.
        left_floor = 0 if left_commands == _FOOTER_LEFT else cell_len(left_commands)

        def fits(result: "Text", allowed_left_rows: int) -> bool:
            lines = result.plain.split("\n")
            need = width_of(result)
            if len(lines) > 2 or need > available:
                return False
            left_width = total - need - _FOOTER_CHROME_COLS
            if left_width < left_floor:
                return False
            return left_width > 0 and footer_left_rows(
                left_commands, left_badge, width=left_width) <= allowed_left_rows

        # Первый проход (левая в один ряд) — только бесплатные ступени 1-2, имя
        # всегда в потолке. Всё платное живёт во втором (левая в два ряда):
        # перебор имени 14 → 5 и его снятие. «Одной строкой» во второй проход не
        # переходит — монотонность, см. докстринг.
        prices_down = [(cols, _RIGHT_PRICES_DOWN) for cols in
                       range(_PROFILE_NAME_MAX_COLS, _PROFILE_NAME_MIN_COLS - 1, -1)]
        passes = ((1, [(_PROFILE_NAME_MAX_COLS, _RIGHT_ONE_LINE),
                       (_PROFILE_NAME_MAX_COLS, _RIGHT_PRICES_DOWN)]),
                  (2, [*prices_down, (None, _RIGHT_PRICES_DOWN)]))
        for allowed_left_rows, variants in passes:
            for cols, layout_width in variants:
                result = build(cols, layout_width)
                if fits(result, allowed_left_rows):
                    return result

        # Резерв (ступени 6-8), от богатого к бедному: снимается блок за стрелкой,
        # затем «запрос» (сессия остаётся), затем цены целиком. Точка уровня,
        # факт-модель и плашка контекста остаются всегда.
        #
        # Почему цены вообще стали жертвой (решение владельца, синк 2026-08-20):
        # раньше последняя ступень возвращалась БЕЗУСЛОВНО, и на узком окне левой
        # доставалось меньше нуля колонок — «Ctrl+C/stop» рвался пополам или исчезал
        # совсем. Цену прогона можно посмотреть и потом, а остановить агента больше
        # нечем, поэтому обмен идёт в эту сторону.
        #
        # И почему берётся не первый попавшийся: когда цены стоят на второй строке,
        # ширину правой колонки диктуют ОНИ, а не шапка, — тогда снятие имени профиля
        # и перехода не освобождает ни колонки. Поэтому если место не нашлось ни на
        # одной ступени, берётся вариант с МИНИМАЛЬНОЙ шириной, а среди равных по
        # ширине — первый, то есть самый богатый: жертва принимается только когда
        # она что-то даёт.
        # Ужатие шапки перебирается ЗАНОВО на каждом уровне цен: снятая строка
        # «запрос» освобождает больше, чем имя профиля, и после неё имя часто
        # возвращается на место. Перебирать в другом порядке значило бы отдавать
        # имя навсегда ради экономии, которой хватило бы и без него.
        head_ladder = ((_PROFILE_NAME_MAX_COLS, True), (None, True), (None, False))
        # Ценами жертвуем ТОЛЬКО ради пола левой колонки. Когда слева обычные
        # F-подсказки, пола нет и терять цены не за что: место всё равно достанется
        # подсказкам, которые не жалко перенести или укоротить.
        price_ladder = (({}, {"with_request": False}, {"with_prices": False})
                        if left_floor else ({},))
        reserve = [build(cols, _RIGHT_PRICES_DOWN, with_transition=transition, **prices)
                   for prices in price_ladder
                   for cols, transition in head_ladder]
        for candidate in reserve:
            if fits(candidate, 2):
                return candidate
        # Не подошло ничего — и дальше важно, ПОЧЕМУ.
        affordable = [c for c in reserve if width_of(c) <= available]
        if affordable:
            # Правая в свою ширину влезает; не хватило ЛЕВОЙ. Её беда снятием
            # имени профиля не лечится — ширину правой диктует строка цен, и
            # шапка на неё не влияет. Отдаём самый узкий из подошедших, а среди
            # равных по ширине — первый, то есть самый богатый.
            return min(affordable, key=width_of)
        # Правая не влезает даже в отведённую ей ширину: её обрежет терминал, и
        # действует прежний контракт вёрстки — самый бедный вариант, где переход
        # снят последним (`test_footer_drops_transition_last_...`).
        return reserve[-1]

    def _footer_total_width(self) -> int:
        """Ширина всего футера — её и делят между собой колонки."""
        try:
            return int(self.app.size.width)
        except Exception:
            return 80

    def _footer_right_available_width(self) -> int:
        """ЖЁСТКИЙ потолок ширины правой колонки: вся строка футера минус хром.

        Не «остаток от левой» (так было до Task 17): #footer-right (width:auto)
        заявляет потребность первым, а сколько при этом останется левой — решает
        лесенка `_compose_footer_right` по числу её рядов."""
        return max(10, self._footer_total_width() - _FOOTER_CHROME_COLS)

    def _usage_segments(self) -> "tuple[Text | None, Text | None]":
        """Раздельные сегменты («запрос», «сессия») для адаптивной вёрстки, или
        (None, None) если показывать нечего.

        Источник — in-memory state исполнителя (не meta-файл): числа появляются
        с первым прогоном сессии, у только что открытой старой сессии их нет.
        Исполнитель сам считает «запрос»: live-дельта текущего хода, пока агент
        работает (сбрасывается с новым вопросом, растёт по мере работы), и итог
        хода, когда он завершён. Оба backend-режима — usage снимается везде."""
        runner = getattr(self.app, "active_runner", None)
        snapshot_fn = getattr(runner, "usage_snapshot", None)
        # Футер не должен ронять экран: нет runner'а / метода / любая ошибка → скрыть.
        try:
            snapshot = snapshot_fn() if callable(snapshot_fn) else None
        except Exception:
            snapshot = None
        if not snapshot:
            return (None, None)
        session_meta, request_meta = snapshot
        cfg = getattr(self.app, "_config", None) or {}
        return usage_segments(
            session_meta, request_meta, cfg.get("pricing"), cfg.get("model"),
            cfg.get("completions_url"),
        )

    def _context_meter(self) -> "Text | None":
        """Rich-Text индикатора контекста или None (скрыт)."""
        cfg = getattr(self.app, "_config", None) or {}
        if str(cfg.get("backend_api_mode")) != "chat_completions":
            return None
        # Калибруем гейдж на ЖЁСТКИЙ порог форс-сжатия — summary_trigger (по нему жмут
        # pre_backend мид-турн и pre-run). turn-end (ratio 0.6) — ПРОАКТИВНАЯ уборка
        # только на границе хода (turn_finish), её показывает нижняя плашка «сжимаю…»,
        # а не 100% на баре. summary_trigger=0 (форс-сжатие выключено) → fallback на
        # turn-end как на единственный реальный порог.
        from summarize.config import (
            parse_summary_trigger_chars,
            parse_summary_turn_end_trigger_chars,
        )
        threshold = parse_summary_trigger_chars(cfg) or parse_summary_turn_end_trigger_chars(cfg)
        if threshold <= 0 or self._store is None or self._active_task_id is None:
            return None
        # Источник — meta.runtime.live_history_chars (persist).
        meta = self._store.get_task_meta(self._active_task_id)
        rt = meta.get("runtime") or {}
        if "live_history_chars" not in rt:
            return None
        try:
            chars = int(rt["live_history_chars"])
        except (TypeError, ValueError):
            return None
        return render_context_meter(chars, threshold)

    def _session_status_word(self) -> "Optional[str]":
        """Сырое слово статуса сессии (без скобок): asking/running/completed/
        stopped/failed — или None, если исход НЕ записан (старая сессия / прогон
        не дошёл до чистого финиша). Единый источник для бейджа трейса (в скобках),
        заголовка окна (без скобок) и строки-итога. None → ничего не выдумываем:
        ни «stopped» в заголовке, ни строки-итога (см. _restore_terminal_indicator)."""
        # ask_human pending — агент ждёт ответа пользователя (приоритет).
        tui_state = getattr(self.app, "tui_state", None)
        if tui_state is not None and tui_state.ask_human_state is not None:
            return "asking"
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and runner.is_running():
            return "running"
        # Нет активного runner — статус из meta (tui-owned, затем legacy top-level).
        if self._store is not None and self._active_task_id is not None:
            meta = self._store.get_task_meta(self._active_task_id)
            tui_status = (meta.get("tui") or {}).get("status")
            if tui_status == "running":
                return "running"
            if tui_status in ("completed", "stopped", "failed"):
                return tui_status
            if meta.get("status") == "running":
                return "running"
            if meta.get("status") == "completed":
                return "completed"
            if meta.get("status") in ("failed", "unknown_failed"):
                return "failed"
        return None   # исход неизвестен — не выдумываем «stopped»

    def _task_status_badge(self) -> "Optional[str]":
        word = self._session_status_word()
        return f"[{word}]" if word else None

    def _refresh_header(self) -> None:
        left, right = self._compose_header_segments()
        try:
            self.query_one("#header-left", Static).update(left)
            self.query_one("#header-right", Static).update(right)
        except Exception:
            pass

    def _footer_left_commands(self) -> str:
        """Базовые команды footer-left без суффикса уведомлений."""
        if self._voice is not None and self._voice.state != "idle":
            # Во время записи слева не команды, а пилюля с таймером и эквалайзером
            # (её рисует `_voice_footer_render`). Лесенка правой колонки меряет левую
            # через `footer_left_rows`, а «пол» ширины считает по этой же строке —
            # значит и то и другое обязано видеть пилюлю: иначе правая колонка мерит
            # команды, которых на экране нет, и выдавливает пилюлю на второй ряд.
            return self._voice_footer_render().plain
        if self.app.tui_state.ask_human_state is not None:
            return "Esc/stop  Enter/ответить"
        if self.app.tui_state.active_task_id is None:
            return _FOOTER_LEFT
        runner = getattr(self.app, "active_runner", None)
        is_running = runner is not None and runner.is_running()
        return _FOOTER_RUNNING_LEFT if is_running else _FOOTER_LEFT

    def _footer_left_text(self) -> str:
        """Команды footer-left + счётчик уведомлений, собранные по фактической ширине.

        Сборку делает footer_left_with_badge: блок уведомлений либо стоит в строке с
        командами (тогда перед ним разделитель-точка), либо уходит вниз целиком и без
        точки. Ширина — контентная ширина самого #footer-left; до первого layout она
        нулевая, и сборка оптимистична — resize/refresh пересоберут по месту (канон
        бренд-карточки: раскладку выбирает фактическая ширина, а не момент сборки)."""
        base = self._footer_left_commands()
        badge = format_notices_badge(self._collect_footer_notices())
        try:
            width = self.query_one("#footer-left", Static).content_size.width
        except Exception:
            width = 0
        return footer_left_with_badge(base, badge, width=width)

    def _refresh_notices_footer(self) -> None:
        """Обновить счётчик уведомлений в footer-left (без затрагивания footer-right)."""
        if self._footer_hint_active:
            return
        # Во время голосовой стадии footer-left занят пилюлей — событийный
        # runtime-notice не должен её затирать (тик записи восстановит счётчик).
        if self._voice is not None and self._voice.state != "idle":
            return
        try:
            self.query_one("#footer-left", Static).update(self._footer_left_text())
        except Exception:
            pass

    def _refresh_footer(self) -> None:
        """Switch footer text between empty-state and main-screen based on active task.

        Left-side update пропускается, если активен Esc-confirm hint — иначе
        тиковый poll затирал бы подсказку до её 2-секундного таймера.
        Right-side (модель·контекст) обновляется каждый тик — метр контекста
        растёт по ходу прогона.
        """
        try:
            self.query_one("#footer-right", Static).update(self._compose_footer_right())
        except Exception:
            pass
        try:
            left = self.query_one("#footer-left", Static)
        except Exception:
            return
        # Голосовая пилюля вытесняет остальной footer-left, пока стадия активна.
        if self._voice is not None and self._voice.state != "idle":
            left.update(self._voice_footer_render())
            return
        if not self._footer_hint_active:
            left.update(self._footer_left_text())

    def on_screen_resume(self) -> None:
        """После возврата с под-экрана — освежить карточку и футер уведомлений."""
        self._refresh_notices_card()
        self._refresh_footer()

    def _current_cwd(self) -> str:
        """Рабочая папка сессии — фиксирована = папка запуска процесса."""
        return str(Path.cwd())

    def _show_footer_hint(self, text: str, seconds: float = 2.0) -> None:
        """Короткая подсказка в footer-left (esc-confirm — 2с, paste-hint — 5с)."""
        self._footer_hint_active = True
        self.query_one("#footer-left").update(text)
        if self._footer_hint_timer is not None:
            self._footer_hint_timer.stop()
        self._footer_hint_timer = self.set_timer(seconds, self._clear_footer_hint)

    def _show_esc_hint(self, kind: str) -> None:
        """Esc-confirm hint: запоминает kind (для 2-го Esc) + показывает подсказку."""
        self._esc_confirm_kind = kind
        self._show_footer_hint(_HINT_TEXT[kind])

    def _clear_footer_hint(self) -> None:
        """Сбросить hint-state (esc + paste) + восстановить footer-left (команды)."""
        self._esc_confirm_kind = None
        self._footer_hint_active = False
        if self._footer_hint_timer is not None:
            self._footer_hint_timer.stop()
            self._footer_hint_timer = None
        self._refresh_footer()

    # --- голосовой ввод (Ctrl+T) ---------------------------------------

    def _voice_config(self) -> dict:
        """voice-секция + прокси агента: у голоса своего поля прокси нет, а в облако
        он ходит по тем же правилам, что backend (копия — оригинал не трогаем)."""
        cfg = getattr(self.app, "_config", None) or {}
        voice = dict(cfg.get("voice") or {})
        voice.setdefault("proxy", cfg.get("proxy"))
        return voice

    def _ensure_voice_flow(self):
        """Ленивая сборка flow (импорты voice-модулей — только по факту)."""
        if self._voice is not None:
            return self._voice
        from tui.services.voice_input import VoiceInputFlow
        from voice.recorder import MicRecorder
        from voice.transcriber import transcribe
        self._voice = VoiceInputFlow(
            self._voice_config(),
            recorder_factory=MicRecorder,
            transcribe=transcribe,
            ui_call=self.app.call_from_thread,
            on_state=self._on_voice_state,
            on_text=self._insert_transcribed,
            on_error=self._on_voice_error,
        )
        return self._voice

    def config_reloaded(self) -> None:
        """Hot-reload конфига (app.reload_config): сбросить кэш voice-flow — он держит
        voice-секцию, снятую при сборке, и без сброса смена endpoint'а в /settings
        не подействовала бы до перезапуска.

        Живую запись не рвём — сброс кэша под работающим рекордером осиротил бы поток
        транскрибации. Но и не забываем: помечаем flow устаревшим, и он уйдёт, как
        только вернётся в idle (`_on_voice_state`). Раньше здесь стоял голый `return`,
        и обещание докстринга «пересоберётся на следующем Ctrl+T» не исполнял никто —
        диктовки до конца сессии шли на прежний адрес (находка ревью коллеги)."""
        if self._voice is None:
            return
        if self._voice.state != "idle":
            self._voice_flow_stale = True
            return
        self._drop_voice_flow()

    def _drop_voice_flow(self) -> None:
        """Снять кэш flow и его тик: следующая диктовка соберёт его на свежем конфиге."""
        self._voice = None
        self._voice_flow_stale = False
        if self._voice_timer is not None:
            self._voice_timer.stop()
            self._voice_timer = None

    def _voice_toggle(self) -> None:
        from config_modules.voice import voice_enabled
        try:
            inp = self.query_one("#cmdinput", CommandTextArea)
        except Exception:
            return
        if inp.disabled or self._any_picker_open:
            return
        if self._voice is not None and self._voice.state != "idle":
            self._voice.toggle()
            return
        if not voice_enabled(self._voice_config()):
            self.notify(
                "голосовой ввод не настроен — /settings → Голосовой ввод",
                severity="warning",
            )
            return
        from voice.recorder import available
        if not available():
            self.notify(
                "голосовой ввод: аудио-стек sounddevice недоступен "
                "(Linux: установите libportaudio2)",
                severity="warning",
            )
            return
        if self._voice_local_mode():
            # Локальный сервис мог умереть после старта TUI: OOM, `gc-voice stop` из
            # соседнего окна, ручной kill, гонка light-switch. `ensure_voice_service`
            # зовут только при старте и из экранов настройки, поэтому выхода из этого
            # состояния не было вовсе — Ctrl+T записывал в пустоту и терял надиктованное
            # на распознавании (находка ревью коллеги). Проверяем ПЕРЕД записью.
            self._start_recording_after_service_check()
            return
        self._ensure_voice_flow().toggle()

    def _voice_local_mode(self) -> bool:
        """Голос настроен на нашу батарейку (а не на облачный endpoint).

        Дёшево и без сети: `status_cheap` смотрит state.json и конфиг."""
        try:
            from tui.services.voice_setup import status_cheap
            cfg = getattr(self.app, "_config", None) or {}
            return status_cheap(cfg).mode == "local"
        except Exception:
            return False

    def _start_recording_after_service_check(self) -> None:
        """Померить порт ФОНОМ и по результату начать запись либо поднять сервис.

        Проверка дешёвая (TCP-connect на loopback), но на мёртвом порту сгорает полным
        таймаутом — в UI-потоке это заметный фриз, поэтому воркер (канон живости
        `/settings`). Подъём тем более: холодный старт — до полутора минут."""
        if self._voice_service_pending:
            return                      # проверка/подъём уже идут — второй не заводим
        self._voice_service_pending = True

        def _work() -> None:
            from tui.services.voice_setup import probe_alive
            try:
                alive = probe_alive()
            except Exception:
                alive = None
            self.app.call_from_thread(self._on_voice_service_checked, alive)

        self.run_worker(_work, thread=True, name="voice-service-check")

    def _on_voice_service_checked(self, alive) -> None:
        self._voice_service_pending = False
        if alive:
            self._ensure_voice_flow().toggle()
            return
        self.notify("голосовой сервис не отвечает — поднимаю…", severity="warning")
        self._raise_voice_service()

    def _raise_voice_service(self) -> None:
        """Поднять батарейку фоном. Запись не начинаем сами: холодный старт долгий, и
        человек за это время успевает уйти — зовём его обратно тостом."""
        self._voice_service_pending = True
        cfg = getattr(self.app, "_config", None) or {}

        def _work() -> None:
            from voice_runtime.autostart import ensure_voice_service
            try:
                ok = ensure_voice_service(cfg)
            except Exception:
                ok = False
            self.app.call_from_thread(self._on_voice_service_raised, ok)

        self.run_worker(_work, thread=True, name="voice-service-raise")

    def _on_voice_service_raised(self, ok: bool) -> None:
        self._voice_service_pending = False
        if ok:
            self.notify("голосовой сервис поднят — можно диктовать", severity="information")
        else:
            self.notify("не удалось поднять голосовой сервис — `gc-voice status` покажет,"
                        " что с ним", severity="warning")

    def _voice_footer_render(self):
        """Пилюля + dim-хвост клавиш (хвост — только на записи)."""
        # Кадр анимации — по стадии. Записи частый тик нужен эквалайзеру, но мигание
        # «● REC» отмерено в кадрах и на ускоренном тике задрожало бы: ему отдаём
        # каждый второй. Волне распознавания частые кадры, наоборот, в плюс — она
        # летит дробными позициями, и чем мельче шаг, тем ровнее полёт.
        frame = (self._voice_frame if self._voice.state == "transcribing"
                 else self._voice_frame // 2)
        pill = render_pill(self._voice.state, self._voice.elapsed_seconds,
                           frame, bands=self._voice.input_bands)
        if self._voice.state == "recording":
            pill.append("  Ctrl+T / Enter — стоп · Esc — отмена", style=_DIM)
        return pill

    def _on_voice_state(self, state: str) -> None:
        try:
            row = self.query_one("#input-row", Horizontal)
            row.set_class(state == "recording", "recording")
        except Exception:
            pass
        if state == "idle":
            if self._voice_timer is not None:
                self._voice_timer.stop()
                self._voice_timer = None
            if self._voice_flow_stale:
                # Конфиг перечитали, пока шла запись: рвать её было нельзя, а теперь
                # можно — следующая диктовка соберётся на свежей voice-секции.
                self._drop_voice_flow()
        elif self._voice_timer is None:
            self._voice_frame = 0
            # 0.08 с (было 0.15): эквалайзер обязан успевать за голосом, а звук теперь
            # приходит порциями по 32 мс. Расчёт кадра дешёвый и вне звукового потока.
            self._voice_timer = self.set_interval(0.08, self._voice_tick)
        if state == "transcribing":
            self._voice_frame = 0   # эмиттер rec-режима стартует с пустой ленты
        self._refresh_footer()

    def _voice_tick(self) -> None:
        if self._voice is None or self._voice.state == "idle":
            return
        self._voice_frame += 1
        self._voice.tick()          # авто-стоп max_seconds
        self._refresh_footer()

    def _on_voice_error(self, message: str) -> None:
        self.notify(message, severity="warning")

    def _insert_transcribed(self, text: str) -> None:
        """Вставить распознанный текст в позицию курсора (+ авто-пробел)."""
        try:
            inp = self.query_one("#cmdinput", CommandTextArea)
        except Exception:
            return
        row, col = inp.cursor_location
        if col > 0:
            line = inp.document.get_line(row)
            if col <= len(line) and line[col - 1] not in (" ", "\t"):
                text = " " + text
        inp.insert(text)
        inp.focus()

    def on_mount(self) -> None:
        self.query_one("#cmdinput", CommandTextArea).focus()
        self._show_brand()
        self._render_startup_notices()
        # Per-project история input-реплик: project_dir известен со старта
        # (threads_dir.parent), attach не нужен — ↑/↓ работают уже в empty-state.
        from tui.services.input_history import InputHistory
        threads_dir = Path(self.app._threads_dir or "")
        project_dir = threads_dir.parent if threads_dir.parts else Path.cwd()
        self._history = InputHistory(project_dir)
        self._history.load()
        self.set_interval(0.25, self._poll_active)   # 250 ms — single source of truth
        # Прогрев executor'а (incl. MCP-дискавери) в фоне на старте — чтобы MCP
        # грузились при запуске gc-ua, а не на первом сообщении. Индикатор —
        # из _poll_active. Build-once: первый start() переиспользует собранный.
        # warm — опциональная оптимизация: прод-runner (AgentRunner) её имеет,
        # тест-дабли раннера могут не иметь — тогда просто пропускаем.
        runner = self.app.get_runner()
        warm = getattr(runner, "warm", None)
        if callable(warm):
            warm()
        self._sync_indicator_reserve()   # пустой старт → резерв бара 2 (без лишней строки)

    def _reset_render_state(self) -> None:
        """Clear all per-task rendering state. Called on task switch and stream_reset."""
        self._task_events.clear()
        self._last_agent_summary = None
        self._next_no = 1
        self._stick_bottom = True   # новый/переключённый трейс → показываем низ (latest)
        self._last_auto_y = 0
        self._active_calls.clear()
        self._subagent_progress.reset()
        self._nested_tool_rows.clear()
        self._nested_widgets_by_parent.clear()
        self._nested_anchor.clear()
        self._subagent_parent_rows.clear()
        self._submit_solution_call_ids.clear()
        self._submit_solution_widgets.clear()
        self._summary_body_widgets.clear()
        self._summary_marker_widgets.clear()
        self._pending_user_widgets.clear()  # spec 2026-05-26
        self._pending_wire_cache.clear()
        self._bang_blocks.clear()
        self._bang_open_block = None
        # clear ask_human state (banner lives in #root, not #trace)
        if self._ask_human_banner is not None:
            try:
                self._ask_human_banner.remove()
            except Exception:
                pass
            self._ask_human_banner = None
            self._ask_human_call_id = None
            if self.app.tui_state.ask_human_state is not None:
                self.app.tui_state.ask_human_state = None
        self._ask_human_question_row = None  # виджет ушёл вместе с trace-очисткой
        self._ask_human_owner_runner = None
        self._ask_human_child_parent_call_id = None
        # I2: cancel_confirm всегда (даже если баннер не монтировался).
        _reset_runner = getattr(self.app, "active_runner", None)
        if _reset_runner is not None and hasattr(_reset_runner, "cancel_confirm"):
            try:
                _reset_runner.cancel_confirm()
            except Exception:
                pass
        if self._confirm_active:
            try:
                self._hide_confirm_banner()
            except Exception:
                pass
        if self._thinking is not None:
            try:
                self._thinking.remove()
            except Exception:
                pass
            self._thinking = None
        self._clear_stream_block()
        self._thinking_started_at = None
        # Сброс эвристики «последнее событие — agent»: на смене задачи / stream_reset
        # история событий обнуляется, индикатор должен начать с чистого состояния.
        import time as _time
        self._last_event_at = _time.monotonic()
        self._last_event_kind = None

    def _session_lock_acquire(self, task_id: str) -> bool:
        """Exclusive attach: True если стали holder, иначе notify и False."""
        threads_dir = self.app._threads_dir or ""
        tui_id = getattr(self.app, "tui_id", "") or ""
        if not threads_dir or not tui_id:
            return True
        if try_acquire(threads_dir, self.app._system, task_id, tui_id):
            return True
        self.app.notify(BUSY_MESSAGE, severity="warning")
        return False

    def _session_lock_release(self, task_id: Optional[str] = None) -> None:
        """Best-effort снять свой lock (уход с tid / exit)."""
        tid = task_id if task_id is not None else self._active_task_id
        if not tid:
            return
        threads_dir = self.app._threads_dir or ""
        tui_id = getattr(self.app, "tui_id", None)
        if not threads_dir:
            return
        release(threads_dir, self.app._system, tid, tui_id)

    def _session_lock_heartbeat(self) -> None:
        """Throttle ≤ 1 Гц, только при живом active_task_id."""
        tid = self._active_task_id
        if not tid:
            return
        threads_dir = self.app._threads_dir or ""
        tui_id = getattr(self.app, "tui_id", "") or ""
        if not threads_dir or not tui_id:
            return
        self._session_lock_hb_mono = heartbeat(
            threads_dir, self.app._system, tid, tui_id,
            last_mono=self._session_lock_hb_mono,
        )

    def attach_active_task(self, task_id: str, *, is_resume: bool = False) -> bool:
        """Attach a thread for polling. Initial state loads via read_all_events.

        is_resume=True (переключение/возобновление сессии через пикер): промпт
        заголовка АВТОРИТЕТНО берётся из стора (даже если None — у пустой сессии).
        is_resume=False (auto-create после send): не затираем только что
        отправленный _resumed_prompt, если стор ещё не успел записать сообщение.

        Returns False если сессия занята другим TUI (не attach).
        """
        prev = self._active_task_id
        if not self._session_lock_acquire(task_id):
            return False
        if prev and prev != task_id:
            self._session_lock_release(prev)
        self._active_task_id = task_id
        self.app.tui_state.active_task_id = task_id
        self._session_lock_hb_mono = 0.0
        self._reset_render_state()
        self._store = FileTaskStore(
            threads_dir=self.app._threads_dir or "",
            system=self.app._system,
        )
        # Настройки режимов принадлежат треду (спека 2026-07-20-session-scoped-settings):
        # берём его значения; их нет (старый тред) — сессия возвращается к умолчанию.
        task_meta = self._store.get_task_meta(task_id)
        saved = read_from_meta(task_meta)
        # Предохранитель моделей (спека §8) — тоже владение треда: restore ЦЕЛИКОМ
        # заменяет breaker окна под новый тред (старое состояние не должно течь).
        self.app.load_circuit_breaker(read_breaker_from_meta(task_meta))
        # Пересобираем executor только при РЕАЛЬНОЙ смене набора: attach идёт и на
        # каждом автосозданном треде, и на возврате к тому же — прогретый раннер
        # (с уже пройденным MCP-дискавери) выбрасывать зря нельзя.
        if self.app.load_session_overrides(saved or {}):
            self.app.invalidate_runner()
        # Сессионный выбор профиля, ставший битым под текущей базой (§9.4) — снять
        # + notice + пересборка; санация ПОСЛЕ load_session_overrides — проверяет то,
        # что реально стало активным оверрайдом этого треда.
        self.app.sanitize_session_profile()
        # read_all_events — атомарный снимок live-файла + cursor (как раньше).
        # Была ли компакция, определяем по голове live-снимка (после turn_end
        # первая запись = сводка) — без доп. I/O. Тред без компакции рендерим
        # старым путём из ЭТОГО снимка (consistent с cursor, без лишних чтений —
        # иначе перетасовка тайминга ломает гонку optimistic-reply). read_segments
        # (glob архивов + повторное чтение) — только для сжатых тредов.
        records, cursor = self._store.read_all_events(task_id)
        compacted = bool(records) and self._store.is_compaction_summary(records[0])
        self._cursor = cursor
        # Очистить #trace от предыдущей сессии — иначе при переходе
        # session A → session B (через /sessions) трейсы склеиваются
        # (виджеты A остаются, B дорисовывается поверх).
        self._clear_trace()
        if compacted:
            self._render_segments(self._store.read_segments(task_id))
        else:
            self._render_records(records)
        self._merge_sidecar_nested(task_id)
        self._refresh_header()
        self._refresh_footer()
        sp = self._session_last_prompt()
        if is_resume:
            self._resumed_prompt = sp            # авторитетно из стора (переключение)
            self._restore_terminal_indicator()   # строка-итог прошлого хода
        elif sp is not None:
            self._resumed_prompt = sp            # auto-create: не затирать свежий send
        self._update_terminal_title()   # статус + последний промпт сессии в таб
        self._sync_indicator_reserve()   # активная задача → резерв бара 3 (отступ снизу)
        self._mount_failed_from_meta(task_id)

        # Свести ask_human-баннер с реальностью: replay/компакция могли оставить
        # баннер для уже отвеченного вопроса (его output суммаризован) — см. helper.
        # Висячий вопрос (реально последний, без живого агента) → remnant с меткой.
        self._reconcile_ask_human_after_render(allow_remnant=True)
        return True

    def on_unmount(self) -> None:
        """App exit / снятие экрана — отпустить lock удерживаемого tid."""
        self._session_lock_release()

    def _mount_failed_from_meta(self, task_id: str) -> None:
        """После рестарта: смонтировать виджеты failed-реплик из
        meta.tui.pending_user_messages — но только truly-orphaned (не нашли
        соответствующего user-сообщения ни в meta.user_intent_log,
        ни в self._task_events).

        Source of truth — meta.user_intent_log (persisted across
        summarizations; user_ask-записи); legacy meta.user_messages_log и
        _task_events — safety net на случай пустого/старого log."""
        if self._store is None:
            return
        from tui.services.relative_time import parse_iso_utc
        meta = self._store.get_task_meta(task_id)
        records = (meta.get("tui") or {}).get("pending_user_messages", [])
        if not records:
            return
        # Primary source: user_intent_log в meta (intent-модель master) — user_ask записи.
        delivered_wires = {
            rec.content.strip()
            for rec in deserialize_list(meta.get("user_intent_log"))
            if rec.action == ACTION_USER_ASK
            and isinstance(rec.content, str)
            and rec.content.strip()
        }
        # Legacy fallback: старый формат meta.user_messages_log (плоские строки).
        delivered_wires.update(
            (s or "").strip()
            for s in (meta.get("user_messages_log") or [])
            if isinstance(s, str)
        )
        # Safety net: live trace (на случай если оба log пусты —
        # старый формат meta, повреждение и т.п.).
        delivered_wires.update(
            ev.summary for ev in self._task_events if ev.kind == "user"
        )
        # Устаревание: самый поздний ts доставленного user_ask. Pending/failed
        # реплику старше него пользователь уже оставил позади (есть доставленные
        # позже) → она устарела, показывать/хранить её не нужно.
        latest_delivered_ts = None
        for rec in deserialize_list(meta.get("user_intent_log")):
            if rec.action != ACTION_USER_ASK:
                continue
            t = parse_iso_utc(getattr(rec, "ts", None))
            if t is not None and (latest_delivered_ts is None or t > latest_delivered_ts):
                latest_delivered_ts = t
        truly_orphaned: list[dict] = []
        kept_records: list[dict] = []
        for r in records:
            wire = r.get("wire_content") or ""
            content = r.get("content") or ""
            # Считаем доставленным, если совпал либо clean content (новый формат,
            # user_messages_log хранит display_content), либо wire (старый формат,
            # где wire == content, или intent_log хранит wire напрямую).
            if (content and content in delivered_wires) or (wire and wire in delivered_wires):
                continue
            # Устарело: есть доставленный user_ask с ts позже этой реплики →
            # пользователь ушёл дальше; не монтируем и вычищаем из meta.
            if latest_delivered_ts is not None:
                r_ts = parse_iso_utc(r.get("ts_utc_iso"))
                if r_ts is not None and r_ts < latest_delivered_ts:
                    continue
            truly_orphaned.append(r)
            kept_records.append(
                {**r, "status": "failed"} if r.get("status") == "pending" else r
            )
        for r in truly_orphaned:
            content = r.get("content") or ""
            if content:
                self._mount_user_reply(content, state="failed")
        if len(kept_records) != len(records) or any(
            r.get("status") == "pending" for r in records
        ):
            self._store.update_tui_meta(
                task_id, {"pending_user_messages": kept_records}
            )

    def _render_segments(self, segments: list[dict]) -> None:
        """Рендер ВСЕХ сегментов сырьём — полный трейс, без схлопывания старых
        сегментов в маркер. Маркер сжатия сегмента i = его compacted_by (ставится
        между сегментами, чтобы было видно, где прошла компакция).

        Дедуп по id: последнее user-сообщение переносится в новый компакт-файл
        (build_preserved_batch), поэтому лежит и в архивном сегменте, и в live —
        в соседних сегментах это дало бы дубль реплики вокруг маркера."""
        from tui.models import TraceEvent
        skipped = collect_skipped_call_ids(
            rec.get("payload")
            for seg in segments
            for rec in seg["messages"]
            if isinstance(rec, dict)
        )
        seen_ids: set[str] = set()
        for i, seg in enumerate(segments):
            for rec in seg["messages"]:
                payload = rec.get("payload")
                mid = payload.get("id") if isinstance(payload, dict) else None
                if mid is not None:
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                self._render_record(rec, skipped_call_ids=skipped)
            comp = seg["compacted_by"]
            if comp is not None:
                ev = TraceEvent(
                    no=self._next_no, event_ts_utc_iso=comp.get("ts", ""),
                    kind="summary", title="summary",
                    summary=build_compaction_label(i + 1, comp),
                    summary_content=comp["synthesis"], full_payload={},
                )
                self._next_no += 1
                self._task_events.append(ev)
                self._render_event(ev)

    def _render_records(self, records: list[dict], *, live: bool = False) -> None:
        skipped = collect_skipped_call_ids(
            rec.get("payload") for rec in records if isinstance(rec, dict)
        )
        for rec in records:
            self._render_record(rec, live=live, skipped_call_ids=skipped)

    def _render_record(
        self, rec: dict, *, live: bool = False, skipped_call_ids: set | None = None
    ) -> None:
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            return
        ev = map_message(payload, no=self._next_no, ts=rec.get("ts", ""),
                         skipped_call_ids=skipped_call_ids)
        # skill_loaded извлекаем ДО early-return: mid-run доставка (display_content="")
        # даёт ev=None, но маркер [skill] всё равно нужен на resume-пути.
        sk = payload.get("skill_loaded")
        if ev is None:
            if sk and not live:
                self._mount_skill_marker(sk, True)  # mid-run доставка: на resume только маркер
            return
        self._next_no += 1
        self._task_events.append(ev)
        self._render_event(ev, live=live)
        # После реплики монтируем [skill]-маркер (если payload несёт skill_loaded).
        # На live-пути маркер уже смонтирован delivery-флоу (_mount_user_reply →
        # _flush_skill_marker → _mount_skill_marker), поэтому только на rebuild.
        if sk and not live:
            self._mount_skill_marker(sk, True)  # resume: восстановить [skill]-маркер (live его уже смонтировал)
        # Spec 2026-05-26 + 2026-05-28: запоминаем kind + время последнего
        # события. Для submit_solution финального ответа — sticky sentinel
        # "_finalized" вместо "agent": блокирует повторный mount в
        # _poll_active, пока не придёт новое значимое событие (gate-reject
        # [policy], новый submit_solution и т.п.).
        import time as _time
        self._last_event_at = _time.monotonic()
        if ev.kind == "agent" and ev.tool_name == "submit_solution":
            self._last_event_kind = "_finalized"
        else:
            self._last_event_kind = ev.kind

    def _show_thinking_indicator(self, *, state: str = "thinking") -> None:
        """Показать индикатор: смонтировать (если ещё нет) или снять `.hidden`.

        Виджет персистентный на всю длительность run'а — анимация не сбрасывается
        при переключениях show/hide во время потока событий. Удаляется только
        в `_destroy_thinking_indicator` (по завершении run'а / reset).

        `state` — "thinking" (активная работа), "validating" (пост-валидация
        после видимого финального ответа) или "compressing" (суммаризация
        истории). Переключается без размонтирования для непрерывного визуала.
        """
        if self._thinking is not None:
            # Индикатор живёт в фиксированной #indicator-bar (вне скролла) —
            # unhide/смена state не двигают вид трейса.
            self._thinking.remove_class("hidden")
            self._thinking.set_state(state)
            return
        _cfg = getattr(self.app, "_config", None) or {}
        indicator = ThinkingIndicator(
            started_at=self._thinking_started_at, state=state,
            sink=getattr(self.app, "_stream_sink", None),
            reasoning_tag=_cfg.get("tui_stream_display", "minimal") != "answer_reasoning",
            reasoning_clock=self._reasoning_clock,
        )
        self._thinking = indicator
        self.query_one("#indicator-bar", Vertical).mount(indicator)

    def _restore_terminal_indicator(self) -> None:
        """На resume сессии: смонтировать строку-итог прошлого хода. Только если
        прогона нет и индикатор ещё не смонтирован.

        Источник: meta.tui.last_indicator (с elapsed). Фолбэк для сессий без него
        (созданы до фичи / другим writer'ом) — персистентный статус
        meta.tui.status / meta.status, маппится в kind, elapsed неизвестен (None →
        «✓ Выполнил» без «за N»)."""
        if self._thinking is not None or self._store is None or self._active_task_id is None:
            return
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and runner.is_running():
            return
        tui = self._store.get_task_meta(self._active_task_id).get("tui") or {}
        last = tui.get("last_indicator")
        if isinstance(last, dict) and last.get("kind") in TERMINAL_INDICATOR_STATES:
            kind = last["kind"]
            elapsed = last.get("elapsed")
        else:
            # Фолбэк: kind из единого источника статуса (того же, что заголовок).
            # None (исход не записан) → строку-итог НЕ рисуем — не выдумываем исход.
            kind = {"completed": "done", "stopped": "cancelled",
                    "failed": "failed"}.get(self._session_status_word())
            if kind is None:
                return
            elapsed = None
        ind = ThinkingIndicator(
            state=kind,
            frozen_elapsed=elapsed if isinstance(elapsed, int) else None,
        )
        self._thinking = ind
        self.query_one("#indicator-bar", Vertical).mount(ind)

    def _hide_thinking_indicator(self) -> None:
        """Скрыть индикатор (CSS `.hidden`), не удаляя — анимация продолжает идти."""
        if self._thinking is None:
            return
        self._thinking.add_class("hidden")

    def _destroy_thinking_indicator(self) -> None:
        """Полностью убрать индикатор. Вызывается когда run завершён."""
        if self._thinking is None:
            return
        self._thinking.remove()
        self._thinking = None

    def _refresh_stream_content(self) -> None:
        """Освежить контент уже смонтированного стрим-виджета из снапшота sink.
        Зовётся poll-веткой (после mount/switch) и событийно (StreamPump → app →
        call_from_thread). НЕ монтирует/снимает/переключает — это poll."""
        # F3: freeze UI на время app-select — не красим стрим и не двигаем stick
        # (здесь зовутся _update_stick_state/_scroll_trace_end). Гейт железный:
        # покрывает и poll, и событийный путь _stream_signal → call_from_thread.
        if self._ui_frozen_for_select():
            return
        sink = getattr(self.app, "_stream_sink", None)
        if sink is None:
            return
        from tui.services.stream_sink import format_stream_block, format_reasoning_line
        from tui.services.markdown_render import render_prose
        self._update_stick_state()
        think, answer = sink.snapshot()
        if self._stream_answer_row is not None:
            self._stream_answer_row.set_content(render_prose(format_stream_block(answer=answer)))
        elif self._stream_block is not None:
            self._stream_block.set_text(format_reasoning_line(think=think), thinking=True)
        else:
            return
        if self._stick_bottom:
            self._scroll_trace_end()

    def _clear_stream_block(self, *, reset_sink: bool = True) -> None:
        """Снять эфемерный StreamBlock и (по умолчанию) сбросить sink.

        reset_sink=False: виджет убирается, но sink не трогается — счётчик и
        reasoning-состояние индикатора продолжают жить (нужно для
        tui.show_*=False: действие в drain без сброса буфера)."""
        if self._stream_block is not None:
            self._stream_block.remove()
            self._stream_block = None
        if self._stream_answer_row is not None:
            self._stream_answer_row.remove()
            self._stream_answer_row = None
        if reset_sink:
            sink = getattr(self.app, "_stream_sink", None)
            if sink is not None:
                sink.reset()

    def _sticky_banner_widget(self):
        """Sticky-баннер над вводом (ask_human / confirm) или None."""
        return self._ask_human_banner or self._confirm_banner

    def _cap_sticky_banner(self, banner, *, chrome: int) -> int:
        """Потолок sticky-баннера: длинный вопрос не выдавливает input/footer.

        Как у input-оверлея — ≤ половины экрана и оставляет min строк трейса.
        Возвращает желаемую (уже ужатую) высоту баннера для расчёта #trace."""
        cap = min(self.size.height // 2,
                  self.size.height - chrome - self._MIN_TRACE_WITH_OVERLAY)
        if cap <= 0:
            return 0
        banner.styles.max_height = cap
        # После layout region уже включает round-border и ≤ cap — самый точный замер.
        # До layout / пока region ещё не ужат: virtual_size = контент без рамки (+2),
        # иначе chrome занижается на 2 и футер уезжает за край экрана.
        region_h = banner.region.height
        if 0 < region_h <= cap:
            return region_h
        try:
            content_h = banner.virtual_size.height
        except Exception:
            content_h = 0
        estimated = (content_h + 2) if content_h > 0 else (len(banner.children) + 2)
        return min(estimated, cap)

    def _sync_trace_max_height(self) -> None:
        """Потолок #trace = экран минус хром #root.

        Без оверлея: #trace `auto` сам сжимается до контента, хром ниже (индикатор/
        ввод/футер) следует за концом контента (Claude-style), пустота — внизу под
        футером; потолок лишь включает скролл при заполнении экрана. БЕЗ замера высоты
        контента → нет покадровой гонки (она давала «прыжок»).

        С открытым input-оверлеем (палитра/пикер): #trace ужимается под его ЖЕЛАЕМУЮ
        высоту (`virtual_size` — полный контент, а НЕ зажатый `region`: region зависит
        от #trace, #trace — от него → петля, оверлей оставался бы вжатым в остаток),
        оставляя оверлею место и min строк трейса; сам оверлей — со своим потолком.

        Sticky-баннер (ask_human/confirm) тоже капается: иначе длинный вопрос
        раздувает chrome и выдавливает поле ввода за край экрана."""
        try:
            root = self.query_one("#root", Vertical)
            trace = self.query_one("#trace", VerticalScroll)
        except Exception:
            return
        overlay = self._open_overlay_widget()
        banner = self._sticky_banner_widget()
        # region.height включает бордеры; скрытые виджеты (display:none) дают 0.
        # Баннер/оверлей исключаем — их желаемую высоту считаем отдельно (иначе
        # ещё не ужатый region баннера раздувает chrome до available<=0).
        chrome = sum(c.region.height for c in root.children
                     if c.id != "trace" and c is not overlay and c is not banner)
        banner_h = 0
        if banner is not None:
            banner_h = self._cap_sticky_banner(banner, chrome=chrome)
        chrome_with_banner = chrome + banner_h
        if overlay is not None:
            # Оверлей ≤ половины экрана (дальше — его скролл) и оставляет ≥ min строк
            # трейса. На малых экранах вторая граница жёстче, на больших — половина.
            cap = min(self.size.height // 2,
                      self.size.height - chrome_with_banner - self._MIN_TRACE_WITH_OVERLAY)
            if cap <= 0:
                return
            overlay.styles.max_height = cap            # потолок оверлея → дальше его скролл
            # Желаемая высота: все наши оверлеи (CommandPalette, SkillPicker,
            # ArgPicker, FilePicker) отдают честный `desired_height` (дети +
            # margin hint'а + рамка; у палитры — ещё докнутый подвал). Фолбэк
            # `len(children)+2` — только для гипотетического оверлея без свойства.
            # len(children) обновляется при фильтре сразу, а virtual_size залипает.
            desired = min(getattr(overlay, "desired_height", len(overlay.children) + 2), cap)
            available = self.size.height - chrome_with_banner - desired
        else:
            available = self.size.height - chrome_with_banner
        if available <= 0:
            return
        if available != self._trace_max_height:
            self._trace_max_height = available
            trace.styles.max_height = available

    _STICK_TOL = 2

    def _trace_is_at_bottom(self) -> bool:
        """Сырой замер: трейс прокручен в самый низ (с допуском). Используется в
        _update_stick_state для повторного «прилипания», когда юзер вернулся к низу."""
        try:
            trace = self.query_one("#trace", VerticalScroll)
        except Exception:
            return True
        return trace.scroll_offset.y >= trace.max_scroll_y - self._STICK_TOL

    def _update_stick_state(self) -> None:
        """Поддержать намерение «липнуть к низу» (_stick_bottom). Сравниваем offset с
        местом, куда сами в прошлый раз прижали (_last_auto_y), а НЕ с max_scroll_y:
        контент растёт вниз, offset при этом не двигается, поэтому добавление контента
        липкость не роняет — её снимает только реальный уход юзера вверх. Возврат к
        низу — возвращает. Вызывать ПЕРЕД прижатием (детект всегда предшествует
        скроллу → юзера не «дёргает» назад при уходе вверх)."""
        try:
            trace = self.query_one("#trace", VerticalScroll)
        except Exception:
            return
        if self._stick_bottom:
            if trace.scroll_offset.y < self._last_auto_y - self._STICK_TOL:
                self._stick_bottom = False
        elif self._trace_is_at_bottom():
            self._stick_bottom = True

    def _scroll_trace_end(self) -> None:
        """Прижать трейс к низу ПОСЛЕ раскладки (height:auto → scroll_end сразу после
        mount() не достаёт до нового низа) и запомнить позицию для _update_stick_state."""
        def _do() -> None:
            try:
                trace = self.query_one("#trace", VerticalScroll)
            except Exception:
                return
            trace.scroll_end(animate=False)
            self._last_auto_y = trace.max_scroll_y
        self.call_after_refresh(_do)

    def _trace_mount(self, widget, *, scroll: bool = True,
                     force_scroll: bool = False) -> None:
        """Смонтировать widget в #trace (индикатор живёт отдельно в #indicator-bar).

        scroll=True: прокрутить вниз, но только если юзер уже внизу
        (`_trace_is_at_bottom`) — иначе не дёргаем его прокрутку. force_scroll=True
        прокручивает безусловно (отправка своего сообщения = re-engage)."""
        trace = self.query_one("#trace", VerticalScroll)
        if force_scroll:
            self._stick_bottom = True   # отправка своего сообщения = re-engage
        trace.mount(widget)
        if scroll and self._stick_bottom:
            self._scroll_trace_end()

    def _on_user_text_submitted(self, text: str) -> None:
        """Обработка отправки user query: обнулить счётчик событий с последнего
        user-send и запомнить текст для cancel-with-undo (Tasks 9-10).
        Дополнительно: append текста в input-history и выход из history-навигации.

        R6 (in-session-rename): авто-имя сессии. normalize → seed/update
        ``last_actual`` в ``tui_meta`` (read-merge-write через стор). Первый
        промпт сеет безусловно; далее — только если ``len(norm) > LAST_ACTUAL_MIN``.
        **Порядок:** запись meta → затем ``_update_terminal_title`` (OSC видит
        свежее имя). best-effort (try/except). ``_resumed_prompt`` остаётся сырым
        для attach/cancel — это НЕ имя сессии (R5: OSC/пикер читают display_name)."""
        self._last_user_send_text = text
        self._resumed_prompt = text   # send = новое последнее сообщение активной сессии (сырой текст)
        self._seed_or_update_last_actual(text)
        self._update_terminal_title()
        self._events_since_last_user_send = 0
        if self._history is not None:
            self._history.append(text)
        self._reset_history_navigation()

    def _seed_or_update_last_actual(self, text: str) -> None:
        """R6: авто-запись ``tui.last_actual`` — человекочитаемое имя сессии.

        normalize → если пусто skip; иначе seed (last_actual пуст → любая длина)
        либо update (len(norm) > LAST_ACTUAL_MIN). best-effort: без стора/активной
        задачи — no-op. Не пишет для slash-команд (путь _dispatch_command).
        """
        if self._active_task_id is None or self._store is None:
            return
        norm = normalize_prompt_title(text)
        if not norm:
            return
        try:
            meta = self._store.get_task_meta(self._active_task_id) or {}
            tui_meta = meta.get("tui") or {}
            current = tui_meta.get("last_actual")
            if isinstance(current, str) and current.strip():
                # уже задано — обновляем только содержательным промптом
                if len(norm) > LAST_ACTUAL_MIN:
                    self._store.update_tui_meta(self._active_task_id, {"last_actual": norm})
            else:
                # seed: первый промпт безусловно
                self._store.update_tui_meta(self._active_task_id, {"last_actual": norm})
        except Exception:
            pass

    def _update_terminal_title(self) -> None:
        """Заголовок терминала = имя сессии + текущий статус (для таба/таскбара).

        R5 (in-session-rename): промпт = display_name(meta.tui) (title >
        last_actual > «Без названия»), а не _resumed_prompt. _resumed_prompt
        остаётся для attach/cancel и НЕ является именем сессии. Статус — live
        (_session_status_word). Дедуп: OSC пишем только при смене — поллер зовёт каждый тик."""
        if self._active_task_id is None:
            title = format_terminal_title()
        else:
            prompt = self._session_display_name()
            title = format_terminal_title(prompt, self._session_status_word())
        if title != self._last_terminal_title:
            self._last_terminal_title = title
            set_terminal_title(title)

    def _session_display_name(self) -> str:
        """display_name(tui_meta) активной задачи — единый источник имени для OSC.

        best-effort: без стора/задачи или при ошибке чтения — фолбэк на
        ``_resumed_prompt`` (старое поведение), чтобы OSC не гас к «Без названия»
        в гонке записи. см. R5/R6 (in-session-rename).
        """
        if self._store is None or self._active_task_id is None:
            return self._resumed_prompt or ""
        try:
            meta = self._store.get_task_meta(self._active_task_id) or {}
            return display_name(meta.get("tui"))
        except Exception:
            return self._resumed_prompt or ""

    def _rename_active_session(self) -> None:
        """R2 (in-session-rename): открыть RenameScreen(current=display_name) активной сессии.

        Гейт: ``_active_task_id`` и ``_store`` не None. Текущее имя =
        ``_session_display_name()`` (title > last_actual > «Без названия»).
        Результат → ``_on_rename_result`` (R3).
        """
        if self._active_task_id is None or self._store is None:
            return
        from tui.screens.rename import RenameScreen
        current = self._session_display_name()
        self.app.push_screen(RenameScreen(current=current), self._on_rename_result)

    def _on_rename_result(self, value: Optional[str]) -> None:
        """R3 (in-session-rename): применить результат RenameScreen.

        ``None`` (Esc) → no-op. Иначе ``update_tui_meta(tid, {"title": value})``
        (``""`` = сброс к авто-имени из last_actual), затем ``_update_terminal_title``
        (OSC сразу видит новое имя). best-effort: без стора/задачи — no-op.
        """
        if value is None:
            return
        if self._active_task_id is None or self._store is None:
            return
        try:
            self._store.update_tui_meta(self._active_task_id, {"title": value})
        except Exception:
            return
        self._update_terminal_title()

    def _session_last_prompt(self) -> "Optional[str]":
        """Последний user-промпт активной сессии из стора (для заголовка при resume).

        Source of truth — meta.user_intent_log (переживает суммаризацию; user_ask-
        записи), как в _mount_failed_from_meta; legacy user_messages_log — фолбэк
        (после сжатия он пуст)."""
        if self._store is None or self._active_task_id is None:
            return None
        try:
            meta = self._store.get_task_meta(self._active_task_id)
        except Exception:
            return None
        for rec in reversed(deserialize_list(meta.get("user_intent_log"))):
            if rec.action == ACTION_USER_ASK and isinstance(rec.content, str) \
                    and rec.content.strip():
                return rec.content
        for s in reversed(meta.get("user_messages_log") or []):
            if isinstance(s, str) and s.strip():
                return s
        return None

    def _reset_thinking_timer(self) -> None:
        """Сбросить таймер «думаю…»: новая точка отсчёта = монотоника сейчас,
        текущий виджет удалён (следующий show создаст свежий с актуальным started_at).
        Также сбрасываем эвристику «agent был последним» — после send юзер ждёт
        ответ заново, индикатор должен показаться без задержки.
        Вызывается на каждом user-send (3 пути отправки)."""
        import time as _time
        now = _time.monotonic()
        self._thinking_started_at = now
        self._last_event_at = now
        self._last_event_kind = None
        self._reasoning_clock.reset()
        self._destroy_thinking_indicator()

    def _render_event(self, ev, *, live: bool = False) -> None:
        if ev.hidden:
            return
        if ev.kind != "user":
            self._events_since_last_user_send += 1

        # Any committed canonical row supersedes the ephemeral reasoning/answer
        # preview for the current step — clear it so it can't drift above new rows.
        # (It re-mounts at the bottom on the next poll tick if streaming continues.)
        self._clear_stream_block()

        # ask_human-output banner cleanup — Фикс B-2.
        # Mapper эмитит для function_call_output name=ask_human либо kind='user'
        # (непустой response), либо kind='system' (пустой/cancel/broken). В обоих
        # случаях снимаем banner ДО передачи в kind-handler, чтобы kind='user'
        # затем нормально реконсилил pending через _pending_wire_match,
        # а kind='system' прошёл в meta-ветку и отрисовал "[ask_human] (без ответа)".
        if (
            self._ask_human_call_id is not None
            and ev.tool_name == "ask_human"
            and ev.kind in ("user", "system")
        ):
            ah_match = (not ev.call_id) or ev.call_id == self._ask_human_call_id
            if ah_match:
                self._unmount_ask_human_banner()
                # НЕ return — событие должно дойти до своего обработчика.

        if ev.kind == "user":
            # New conversation turn — clear retry-tracking from previous turn(s).
            # Previous submit_solutions are now considered completed answers,
            # not retry candidates for collapse.
            self._submit_solution_widgets.clear()

            # Reconcile с pending: матч по WIRE (то, что executor записал в .jsonl как
            # content), а НЕ по ev.summary: у skill-сообщений summary = display_content
            # (чистый текст для отображения), а в кэше pending лежит wire_content.
            polled_wire = ev.summary
            if isinstance(ev.full_payload, dict):
                c = ev.full_payload.get("content")
                if isinstance(c, str):
                    polled_wire = c
            for cid, widget in list(self._pending_user_widgets.items()):
                if self._pending_wire_match(cid, polled_wire):
                    widget.set_state("normal")
                    self._pending_user_widgets.pop(cid, None)
                    self._remove_pending_record(cid)
                    return
            # Без pending-матча — обычный mount (используем display_content = ev.summary).
            self._mount_user_reply(ev.summary, event_no=ev.no)
            return

        if ev.kind == "agent":
            # A1: дубль контента (проза → тот же финализатор / повтор) — молча
            # опускаем второй ряд (контент уже показан первым). Exact-match (strip),
            # без маркера: завершение хода и так видно по готовому инпуту.
            norm = (ev.summary or "").strip()
            if norm and norm == self._last_agent_summary:
                return
            self._last_agent_summary = norm
            if ev.tool_name == "intermediate_reply":
                from tui.services.trace_render import build_intermediate_widgets
                # «Прилипание к низу»: фиксируем ДО монтирования (как у прозы).
                following = self._stick_bottom
                for w in build_intermediate_widgets(ev.summary, event_no=ev.no):
                    self._trace_mount(w, scroll=False)
                if following:
                    self._scroll_trace_end()
                return
            from tui.services.trace_render import build_agent_widgets

            # «Прилипание к низу»: фиксируем ДО монтирования (mount растит max_scroll_y).
            following = self._stick_bottom
            mounted_widgets = build_agent_widgets(ev.summary, event_no=ev.no)
            for w in mounted_widgets:
                self._trace_mount(w, scroll=False)

            # Submit_solution comment как note — после всех виджетов.
            note_widget: Optional["TraceRow"] = None
            if isinstance(ev.full_payload, dict) and ev.full_payload.get("_submit_comment"):
                from tui.widgets.trace_row import TraceRow
                from tui.services.trace_render import note_text
                note_widget = TraceRow(
                    prefix="* ",
                    content=note_text(ev.full_payload["_submit_comment"]),
                    kind_class="line-note",
                    event_no=ev.no,
                    focusable=False,
                )
                self._trace_mount(note_widget, scroll=False)
            if following:
                self._scroll_trace_end()

            if ev.tool_name == "submit_solution" and ev.call_id:
                self._submit_solution_call_ids.add(ev.call_id)
                if self._submit_solution_widgets:
                    # Отклонённые intent_guard'ом попытки убираем молча (как
                    # regenerate в чатах): внутренняя кухня качества юзеру не
                    # показывается, обе попытки сохранены в jsonl треда.
                    for prev_widgets, prev_note in self._submit_solution_widgets:
                        for w in prev_widgets:
                            w.remove()
                        if prev_note is not None:
                            prev_note.remove()
                # Трекаем ВСЕ смонтированные виджеты этой submit_solution
                # (prose-блоки + code-block-panels) — нужно для коллапса при retry.
                self._submit_solution_widgets.append((list(mounted_widgets), note_widget))
                # Финальный ответ виден — переключаем индикатор в режим
                # «✓ завершаю…» немедленно (без 250мс лага на следующий тик
                # _poll_active). Sticky-маркер sentinel выставляется в
                # _render_records, чтобы пережить _last_event_kind = ev.kind ниже.
                if self._thinking is not None:
                    self._thinking.set_state("validating")
            return

        if ev.kind == "ask_human":
            self._on_ask_human_event(ev, live=live)
            return

        if ev.kind == "todo":
            # Чек-лист — пассивный прогресс: TraceRow без running-глифа/result.
            # Когнитивное событие — эпизод раздумья не рвём (как session_think).
            # Канон spec 2026-07-31 §2 — строки собирает общий билдер
            # (он же у read-only канала), здесь только монтируем.
            from tools.subagents.progress import todo_items_from_arguments
            from tui.services.todo_render import build_todo_block
            items = todo_items_from_arguments(
                (ev.full_payload or {}).get("arguments"))
            self._trace_mount(build_todo_block(
                items, subagent=False, indent="   ", event_no=ev.no))
            return

        if ev.kind == "call":
            if ev.tool_name in COGNITIVE_TOOLS:
                # Когнитивный тул продолжает эпизод (без call_id — просто no-op,
                # НЕ рвёт эпизод и не попадает в interrupt).
                if ev.call_id:
                    self._reasoning_clock.start(ev.call_id)
            elif ev.tool_name:
                # Не-когнитивный тул = действие в мире → рвёт эпизод раздумья.
                self._reasoning_clock.interrupt()
            from tui.services.trace_render import args_portion, is_tool_group_start
            row = ToolCallRow(
                call_id=ev.call_id or f"_anon_{ev.no}",
                tool_name=ev.tool_name or "<tool>",
                args_summary=args_portion(ev.summary),
                event_no=ev.no,
                waiting=True,  # batch: · пока очередь execute не дошла
            )
            # Первый tool в группе (предыдущий ряд — не tool) получает верхний
            # отступ; подряд идущие tool-строки остаются компактными.
            if is_tool_group_start(self.query_one("#trace", VerticalScroll)):
                row.add_class("tool-group-start")
            self._active_calls[row.call_id] = row
            # Durable anchor for nested (reopen / after parent result pop).
            if (ev.tool_name or "").startswith("subagent_") and row.call_id:
                self._subagent_parent_rows[row.call_id] = row
            self._trace_mount(row)
            self._reconcile_active_tool_phases()
            return

        if ev.kind in ("result", "error"):
            if ev.call_id:
                self._reasoning_clock.stop(ev.call_id)
            # Suppress output of submit_solution (no `|- ok` after final answer)
            if ev.call_id and ev.call_id in self._submit_solution_call_ids:
                self._submit_solution_call_ids.discard(ev.call_id)
                return
            # Mutate existing row in place
            row = self._active_calls.get(ev.call_id) if ev.call_id else None
            if row is not None:
                row.set_result(
                    summary=ev.summary,
                    error=(ev.kind == "error"),
                )
                # Nested под subagent_* оставляем в живой сессии (иерархия под ✓/✗).
                self._active_calls.pop(ev.call_id, None)
                self._reconcile_active_tool_phases()
                return
            # Fallback (call_id unknown or no matching row) — append separate line
            prefix = "   " if ev.kind == "result" else "   [!] "
            cls = "line-ok" if ev.kind == "result" else "line-error"
            self._mount_trace_row(
                prefix=prefix, content=ev.summary, kind_class=cls,
                event_no=ev.no,
            )
            return

        if ev.kind == "summary":
            from tui.widgets.trace_row import SummaryMarker
            marker_label = ev.summary or "контекст сжат"
            marker = SummaryMarker(
                prefix="▸ ", content=f"──── {marker_label} ────",
                kind_class="summary-marker", event_no=ev.no, focusable=True,
            )
            self._trace_mount(marker)
            self._summary_marker_widgets[ev.no] = marker
            body_text = (ev.summary_content or "").strip()
            if body_text:
                # Свёрнут по умолчанию (.hidden); toggle по клику маркера ИЛИ тела.
                body = SummaryMarker(
                    prefix="   ", content=f"Контекст сохранён:\n{body_text}",
                    kind_class="summary-body hidden", event_no=ev.no, focusable=False,
                )
                self._trace_mount(body)
                self._summary_body_widgets[ev.no] = body
            return

        if ev.kind == "error_msg":
            self._render_error_msg(ev)
            return

        # default: meta. summary вида "[tag] описание" — отрезаем prefix для TraceRow.
        from tui.services.trace_render import TAG_RE
        m = TAG_RE.match(ev.summary)
        if m:
            self._mount_trace_row(
                prefix=f"[{m.group('tag')}] ",
                content=m.group("rest"),
                kind_class="line-meta",
                event_no=ev.no,
            )
        else:
            self._mount_trace_row(
                prefix="", content=ev.summary, kind_class="line-meta", event_no=ev.no,
            )

    def _render_error_msg(self, ev) -> None:
        """error_msg с известным call_id финализирует соответствующую строку-вызов
        (✗ inline, таймер стоп) и подавляет дублирующую [!]-строку. Иначе — [!] как есть."""
        row = self._active_calls.get(ev.call_id) if ev.call_id else None
        if row is not None:
            row.set_result(summary=ev.summary, error=True)
            # Nested под subagent_* оставляем в живой сессии (иерархия под ✓/✗).
            self._active_calls.pop(ev.call_id, None)
            self._reconcile_active_tool_phases()
            return
        self._mount_trace_row(
            prefix="[!] ", content=ev.summary,
            kind_class="line-error", event_no=ev.no,
        )

    def _reconcile_active_tool_phases(self) -> None:
        """В незавершённом batch: первая строка active (▸), остальные waiting (·)."""
        reconcile_batch_phases(list(self._active_calls.values()))

    def _drain_subagent_progress(self) -> None:
        """Слить live-события субагента из app-очереди и отрисовать nested."""
        sink = getattr(self.app, "_subagent_progress_sink", None)
        if sink is None:
            return
        drain = getattr(sink, "drain", None)
        if not callable(drain):
            return
        events = drain()
        if not events:
            return
        self._subagent_progress.apply_drained(
            events,
            mount_nested_tool=self._mount_nested_tool,
            finish_nested_tool=self._finish_nested_tool,
            mount_nested_prose=self._mount_nested_prose,
            parent_heartbeat=self._parent_subagent_heartbeat,
            show_ask_human=self._show_child_ask_human,
            hide_ask_human=self._hide_child_ask_human,
            mount_nested_user=self._mount_nested_user,
            mount_nested_todo=self._mount_nested_todo,
        )

    def _nested_parent_row(self, parent_call_id: str):
        """Parent ToolCallRow for nested mount: active or durable subagent_*."""
        return (
            self._active_calls.get(parent_call_id)
            or self._subagent_parent_rows.get(parent_call_id)
        )

    def _merge_sidecar_nested(self, task_id: str) -> None:
        """После rebuild parent-трейса: смонтировать nested из data_subagents_*."""
        if self._store is None or not task_id:
            return
        records = self._store.read_subagent_trace(task_id)
        if not records:
            return
        actions = plan_sidecar_nested_actions(records)
        apply_sidecar_nested_actions(
            actions,
            mount_nested_tool=self._mount_nested_tool,
            finish_nested_tool=self._finish_nested_tool,
            mount_nested_prose=self._mount_nested_prose,
            mount_nested_summary=self._mount_nested_child_summary,
            mount_nested_user=self._mount_nested_user,
            mount_nested_todo=self._mount_nested_todo,
        )

    def _nested_mount_after(self, parent_call_id: str, widget) -> None:
        """Смонтировать nested-виджет сразу после parent или последнего nested."""
        from textual.containers import VerticalScroll
        parent = self._nested_parent_row(parent_call_id)
        if parent is None:
            return
        trace = self.query_one("#trace", VerticalScroll)
        anchor = self._nested_anchor.get(parent_call_id, parent)
        try:
            trace.mount(widget, after=anchor)
        except Exception:
            trace.mount(widget)
        self._nested_anchor[parent_call_id] = widget
        self._nested_widgets_by_parent.setdefault(parent_call_id, []).append(widget)

    def _mount_nested_tool(
        self,
        *,
        parent_call_id: str,
        tool_name: str,
        tool_call_id: str,
        args_summary: str,
        step: int = 0,
        max_steps: int = 0,
        **_kw,
    ) -> None:
        if self._nested_parent_row(parent_call_id) is None:
            return
        row = ToolCallRow(
            call_id=tool_call_id or f"_nested_{tool_name}_{step}",
            tool_name=tool_name,
            args_summary=args_summary or "",
            nested=True,
            waiting=False,  # tool_start = уже execute
        )
        if tool_call_id:
            self._nested_tool_rows[tool_call_id] = row
        self._nested_mount_after(parent_call_id, row)

    def _finish_nested_tool(
        self,
        *,
        parent_call_id: str,
        tool_call_id: str = "",
        ok: bool = True,
        error_brief: str | None = None,
        **_kw,
    ) -> None:
        row = self._nested_tool_rows.get(tool_call_id) if tool_call_id else None
        if row is None:
            return
        summary = "" if ok else (error_brief or "error")
        row.set_result(summary=summary, error=not ok)

    def _mount_nested_prose(
        self,
        *,
        parent_call_id: str,
        prose: str,
        **_kw,
    ) -> None:
        """Read-only счёт-в-счёт с merge_nested_readonly._prose (trace_render.py)."""
        if self._nested_parent_row(parent_call_id) is None:
            return
        text = (prose or "").strip()
        if not text:
            return
        row = TraceRow(
            prefix=NESTED_PROSE_PREFIX,
            content=text,
            kind_class=LINE_INTERMEDIATE_CLASS,
            event_no=0,
            focusable=False,
        )
        self._nested_mount_after(parent_call_id, row)

    def _mount_nested_todo(
        self,
        *,
        parent_call_id: str,
        tool_call_id: str = "",
        todo_items: list | None = None,
        **_kw,
    ) -> None:
        """Развёрнутый чек-лист субагента (spec 2026-07-31 §3).

        Пассивные todo-строки (не ToolCallRow): в _nested_tool_rows НЕ
        регистрируются, поэтому finish по их call_id — естественный no-op.
        Строки собирает общий билдер (он же у merge_nested_readonly)."""
        if self._nested_parent_row(parent_call_id) is None:
            return
        from tui.services.todo_render import build_todo_block
        self._nested_mount_after(parent_call_id, build_todo_block(
            todo_items or [], subagent=True, indent=NESTED_PREFIX))

    def _mount_nested_user(
        self,
        *,
        parent_call_id: str,
        prose: str,
        **_kw,
    ) -> None:
        """Mid-run user steer under active subagent_* (active-scope input).

        Read-only счёт-в-счёт с merge_nested_readonly._user (trace_render.py)."""
        if self._nested_parent_row(parent_call_id) is None:
            return
        text = (prose or "").strip()
        if not text:
            return
        row = TraceRow(
            prefix=NESTED_USER_PREFIX,
            content=text,
            kind_class=LINE_USER_CLASS,
            event_no=0,
            focusable=False,
        )
        self._nested_mount_after(parent_call_id, row)

    def _mount_nested_child_summary(
        self,
        *,
        parent_call_id: str,
        prose: str = "",
        **_kw,
    ) -> None:
        self._mount_nested_prose(
            parent_call_id=parent_call_id,
            prose=prose or CHILD_SUMMARY_LABEL,
        )

    def _parent_subagent_heartbeat(
        self,
        *,
        parent_call_id: str,
        step: int = 0,
        max_steps: int = 0,
        last_tool: str | None = None,
        **_kw,
    ) -> None:
        row = self._nested_parent_row(parent_call_id)
        if row is None:
            return
        row.set_live_status(
            format_parent_live_status(
                step=step, max_steps=max_steps, last_tool=last_tool,
            )
        )

    def _show_child_ask_human(
        self,
        *,
        parent_call_id: str,
        prose: str,
        via: str | None = None,
        tool_call_id: str | None = None,
        **_kw,
    ) -> None:
        """Баннер ask_human от child: только UI, без строки в parent jsonl/трейсе."""
        from tui.widgets.ask_human_banner import AskHumanBanner
        from tui.state import AskHumanState

        if self._ask_human_banner is not None:
            self._unmount_ask_human_banner()
        # Не монтируем question-row в parent-трейс — HITL child эфемерен.
        self._ask_human_question_row = None
        banner = AskHumanBanner(question=prose, via=via)
        self._ask_human_banner = banner
        self._ask_human_call_id = tool_call_id or parent_call_id
        self._ask_human_owner_runner = self.app.active_runner
        self._ask_human_child_parent_call_id = parent_call_id or None
        self.app.tui_state.ask_human_state = AskHumanState(
            call_id=self._ask_human_call_id or "",
            question=prose,
        )
        # Пауза тикера корневого субагента на время ответа.
        parent_row = self._active_calls.get(parent_call_id)
        if parent_row is not None:
            parent_row.pause()
        inp_row = self.query_one("#input-row", Horizontal)
        self.query_one("#root", Vertical).mount(banner, before=inp_row)
        inp = self.query_one("#cmdinput", CommandTextArea)
        inp.placeholder = "ваш ответ агенту…"
        inp.add_class("ask-human-mode")
        self._refresh_header()
        self._refresh_footer()
        self._update_terminal_title()

    def _hide_child_ask_human(self, *, parent_call_id: str = "", **_kw) -> None:
        """Снять child ask-баннер после ответа; resume тикера корня."""
        parent_row = self._active_calls.get(parent_call_id) if parent_call_id else None
        if parent_row is not None:
            parent_row.resume()
        self._unmount_ask_human_banner()

    
    def _diff_thread_identity(self) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """(threads_dir, system_id, thread_id) текущей сессии для DiffService."""
        threads_dir = getattr(self.app, "_threads_dir", None)
        system_id = getattr(self.app, "_system", None)
        thread_id = self._active_task_id or getattr(
            getattr(self.app, "tui_state", None), "active_task_id", None
        )
        if not isinstance(threads_dir, str) or not threads_dir.strip():
            threads_dir = None
        if not isinstance(system_id, str) or not system_id.strip():
            system_id = None
        if not isinstance(thread_id, str) or not thread_id.strip():
            thread_id = None
        return threads_dir, system_id, thread_id

    def _diff_service_for_preview(self):
        cfg = getattr(self.app, "_config", None) or {}
        launch_root = cfg.get("launch_root")
        if not launch_root:
            return None
        from tui.screens.diff import _service_from_config

        threads_dir, system_id, thread_id = self._diff_thread_identity()
        return _service_from_config(
            str(launch_root),
            cfg,
            threads_dir=threads_dir,
            system_id=system_id,
            thread_id=thread_id,
        )

    def _on_ask_human_event(self, ev, *, live: bool = False) -> None:
        from tui.widgets.ask_human_banner import AskHumanBanner
        if self._ask_human_banner is not None:
            if self._ask_human_call_id == ev.call_id:
                return  # тот же вопрос уже смонтирован (повторный poll)
            # Новый вопрос замещает предыдущий: старый вопрос стал историей —
            # снимаем его баннер (его строка раскрывается), баннер переезжает
            # на новый. Иначе цепочка вопросов без ответа (переформулировки)
            # застряла бы на первом баннере и проглатывала все последующие.
            self._unmount_ask_human_banner()
        # Вопрос всегда оседает в трейсе dim-строкой — история диалога полная
        # и одинаковая в live и replay. Пока баннер виден, строка скрыта (нет
        # дубля на экране); монтируем её сразу, чтобы зафиксировать позицию
        # ВЫШЕ будущего ответа юзера — раскроется при схлопывании баннера.
        from tui.services.trace_render import build_answered_question_widget
        self._ask_human_question_row = build_answered_question_widget(
            ev.summary, event_no=ev.no, extra_classes="ask-human-question hidden",
        )
        self._trace_mount(self._ask_human_question_row, scroll=False)
        banner = AskHumanBanner(question=ev.summary)
        self._ask_human_banner = banner
        self._ask_human_call_id = ev.call_id
        # Владелец — только если плашка пришла из live-поллинга активного run'а
        # (он реально висит в input()). При воспроизведении истории — не трогаем.
        if live:
            self._ask_human_owner_runner = self.app.active_runner
        # update tui_state for header [asking] badge
        from tui.state import AskHumanState
        self.app.tui_state.ask_human_state = AskHumanState(
            call_id=ev.call_id or "", question=ev.summary,
        )
        # mount banner before #input-row; потолок высоты — после layout
        # (_cap_sticky_banner), иначе длинный вопрос выдавливает input/footer.
        inp_row = self.query_one("#input-row", Horizontal)
        self.query_one("#root", Vertical).mount(banner, before=inp_row)
        self.call_after_refresh(self._sync_trace_max_height)
        # switch input to ask-human mode
        inp = self.query_one("#cmdinput", CommandTextArea)
        inp.placeholder = "ваш ответ агенту…"
        inp.add_class("ask-human-mode")
        self._refresh_header()
        self._refresh_footer()
        self._update_terminal_title()
        if self._stick_bottom:
            self._scroll_trace_end()

    def _unmount_ask_human_banner(self) -> None:
        self._ask_human_owner_runner = None
        self._ask_human_child_parent_call_id = None
        if self._ask_human_banner is None:
            return
        # Плашка схлопывается → раскрываем трейс-строку вопроса (история).
        if self._ask_human_question_row is not None:
            try:
                self._ask_human_question_row.remove_class("hidden")
            except Exception:
                pass
            self._ask_human_question_row = None
        self._ask_human_banner.remove()
        self._ask_human_banner = None
        self._ask_human_call_id = None
        self.app.tui_state.ask_human_state = None
        inp = self.query_one("#cmdinput", CommandTextArea)
        inp.placeholder = _PLACEHOLDER_READY
        inp.remove_class("ask-human-mode")
        self._refresh_header()
        self._refresh_footer()
        self._update_terminal_title()

    # ──────────────────────── confirm banner (T2) ───────────────────────────

    def show_confirm_banner(
        self,
        title: str,
        question: str,
        fields: list | None = None,
        via: str | None = None,
    ) -> None:
        """Смонтировать ConfirmBanner и переключить в режим одиночной клавиши.

        Вызывается на UI-потоке (напр. через App.call_from_thread из T3).
        Скрывает #input-row, убирает фокус с TextArea — «y»/«n» доходят до
        Screen.on_key и не поглощаются TextArea. fields — структурированные
        детали решения (Tool/Path/Command/Reason/Risk) из confirm-порта.
        via — метка субагента для child HITL (опционально)."""
        from tui.widgets.confirm_banner import ConfirmBanner
        if self._confirm_active:
            self._hide_confirm_banner()
        banner = ConfirmBanner(
            banner_title=title, question=question, fields=fields, via=via,
        )
        self._confirm_banner = banner
        self._confirm_active = True
        inp_row = self.query_one("#input-row", Horizontal)
        self.query_one("#root", Vertical).mount(banner, before=inp_row)
        self.call_after_refresh(self._sync_trace_max_height)
        inp_row.add_class("hidden")
        # Инпут на время confirm: blur + СИНХРОННО disabled. Одного set_focus(None)
        # мало — он применяется асинхронно (след. цикл pump), и под нагрузкой первый
        # «y» успевает уйти в ещё-сфокусированную TextArea ДО blur (плавающий баг).
        # disabled=True снимает виджет с обработки клавиш немедленно — гонки нет.
        try:
            self.query_one("#cmdinput", CommandTextArea).disabled = True
        except Exception:
            pass
        self.set_focus(None)
        if self._stick_bottom:
            self._scroll_trace_end()

    def _hide_confirm_banner(self) -> None:
        """Снять ConfirmBanner и восстановить поле ввода."""
        self._confirm_active = False
        if self._confirm_banner is not None:
            try:
                self._confirm_banner.remove()
            except Exception:
                pass
            self._confirm_banner = None
        try:
            self.query_one("#input-row", Horizontal).remove_class("hidden")
        except Exception:
            pass
        try:
            inp = self.query_one("#cmdinput", CommandTextArea)
            inp.disabled = False  # снять disable ДО focus (фокус не идёт на disabled)
            inp.focus()
        except Exception:
            pass

    def _answer_confirm(self, choice: str) -> None:
        """Отправить ответ в confirm-канал раннера и скрыть баннер."""
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and hasattr(runner, "submit_confirm_reply"):
            runner.submit_confirm_reply(choice)
        self._hide_confirm_banner()

    # ──────────────────────────────────────────────────────────────────────────

    def _ask_human_target_runner(self):
        """Раннер-владелец текущего ask_human, если он жив и совпадает с активным
        (значит реально висит в input() на этом вопросе) — иначе None (ответ
        пойдёт новым ходом)."""
        owner = self._ask_human_owner_runner
        runner = getattr(self.app, "active_runner", None)
        if owner is not None and owner is runner and runner.is_running():
            return owner
        return None

    def _ask_human_dialog_continued(self) -> bool:
        """True, если после смонтированного ask_human-вопроса в трассе есть
        осмысленные события (ответ / 'контекст сжат' / продолжение) — значит вопрос
        НЕ висячий (его output мог быть суммаризован при компакции), remnant не нужен.
        False — ask_human реально последнее осмысленное событие (висячий вопрос)."""
        cid = self._ask_human_call_id
        ah_idx = None
        for i, ev in enumerate(self._task_events):
            if ev.kind == "ask_human" and (not cid or ev.call_id == cid):
                ah_idx = i
        if ah_idx is None:
            return False
        meaningful = {"user", "agent", "call", "result", "error", "error_msg", "summary"}
        return any(ev.kind in meaningful for ev in self._task_events[ah_idx + 1:])

    def _reconcile_ask_human_after_render(self, *, allow_remnant: bool) -> None:
        """Свести ask_human-баннер с реальностью после replay / re-render.

        Баннер мог остаться смонтирован, хотя вопрос уже отвечен, а его output —
        в заархивированном/суммаризированном сегменте (компакция), поэтому B-2
        cleanup не сработал. Логика:
        - диалог продолжился после вопроса → тихо снять баннер (без remnant);
        - вопрос реально последний и живого агента нет → висячий: снять баннер и
          (в attach-контексте, allow_remnant) показать человекочитаемую метку;
        - живой агент ждёт ответа → баннер валиден, не трогаем."""
        if self._ask_human_banner is None:
            return
        if self._ask_human_dialog_continued():
            self._unmount_ask_human_banner()
            return
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and runner.is_running():
            return  # живой агент действительно ждёт ответа
        self._unmount_ask_human_banner()
        if allow_remnant:
            # Текст вопроса не дублируем: он уже в трейсе серой рамкой «? …»
            # (её монтирует _on_ask_human_event через build_answered_question_widget).
            self._mount_trace_row(
                prefix="Агент спрашивал (прервано) — ",
                content="продолжите сообщением или Esc для выхода",
                kind_class="line-meta",
            )

    def _mount_user_reply(
        self,
        text: str,
        *,
        event_no: int = 0,
        state: str = "normal",
        client_id: str | None = None,
    ):
        from tui.widgets.user_reply import UserReplyContainer
        widget = UserReplyContainer(text, event_no=event_no, state=state)
        # pending = свежая отправка юзера → форсим скролл вниз (re-engage);
        # normal/failed = воспроизведение из истории → обычное прилипание.
        self._trace_mount(widget, force_scroll=(state == "pending"))
        if state == "pending" and client_id is not None:
            self._record_pending_widget(client_id, widget)
        if state == "pending":
            self._flush_skill_marker()
        return widget

    def _mount_skill_marker(self, skill_id: str, loaded: bool) -> None:
        """Серый [skill]-маркер итога доставки скилла."""
        content = (f"`{skill_id}` загружен" if loaded
                   else f"`{skill_id}` не найден — инструкция не подгружена")
        row = self._mount_trace_row(prefix="[skill] ", content=content, kind_class="line-meta")
        row.add_class("skill-marker")

    def _flush_skill_marker(self) -> None:
        """Смонтировать отложенный маркер скилла (выставлен в
        _consume_pending_prefix), если есть. Вызывается после реплики юзера."""
        m = self._pending_skill_marker
        self._pending_skill_marker = None
        if m is None:
            return
        skill_id, loaded = m
        self._mount_skill_marker(skill_id, loaded)

    _PENDING_CAP = 20

    def _track_pending(self, display_text: str, wire_content: str) -> str:
        """Записать pending-реплику в meta.tui + положить в `_pending_user_widgets`.

        display_text — то, что показывает виджет (как ввёл юзер).
        wire_content — то, что executor запишет в .jsonl (может отличаться при
        наличии pending skill/cwd prefix). Используется для reconciliation.
        Возвращает client_id.
        """
        tid = self.app.tui_state.active_task_id
        if tid is None or self._store is None:
            # Под фолбэк — генерим client_id, но в meta не пишем (нет ни задачи, ни стора).
            cid = uuid.uuid4().hex
            self._last_pending_client_id = cid
            return cid

        client_id = uuid.uuid4().hex
        record = {
            "client_id": client_id,
            "content": display_text,
            "wire_content": wire_content,
            "ts_utc_iso": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        existing = (self._store.get_task_meta(tid).get("tui") or {}).get(
            "pending_user_messages", []
        )
        new_list = (existing + [record])[-self._PENDING_CAP:]
        self._store.update_tui_meta(tid, {"pending_user_messages": new_list})
        # In-memory кэш для reconciliation: meta может быть перетёрт executor'ом.
        self._pending_wire_cache[client_id] = wire_content
        self._last_pending_client_id = client_id
        return client_id

    def _record_pending_widget(self, client_id: str, widget) -> None:
        """Связать только что смонтированный pending-виджет с его client_id."""
        self._pending_user_widgets[client_id] = widget

    def _remove_pending_record(self, client_id: str) -> None:
        """Удалить запись из meta.tui.pending_user_messages по client_id."""
        self._pending_wire_cache.pop(client_id, None)
        tid = self.app.tui_state.active_task_id
        if tid is None or self._store is None:
            return
        existing = (self._store.get_task_meta(tid).get("tui") or {}).get(
            "pending_user_messages", []
        )
        new_list = [r for r in existing if r.get("client_id") != client_id]
        if len(new_list) != len(existing):
            self._store.update_tui_meta(tid, {"pending_user_messages": new_list})

    def _mark_pending_failed(self) -> None:
        """При фейле run'а: все текущие pending-виджеты → state=failed,
        записи в meta.tui.pending_user_messages → status=failed.
        Очищает self._pending_user_widgets (виджеты остаются на экране как failed)."""
        if not self._pending_user_widgets:
            return
        tid = self.app.tui_state.active_task_id
        for cid, widget in self._pending_user_widgets.items():
            widget.set_state("failed")
        if tid is not None and self._store is not None:
            records = (self._store.get_task_meta(tid).get("tui") or {}).get(
                "pending_user_messages", []
            )
            failed_cids = set(self._pending_user_widgets.keys())
            new_list = [
                {**r, "status": "failed"} if r.get("client_id") in failed_cids else r
                for r in records
            ]
            self._store.update_tui_meta(tid, {"pending_user_messages": new_list})
        self._pending_user_widgets.clear()

    def _begin_auto_created_task(self) -> str:
        """Автосоздание треда под первое сообщение: id, store, наследование
        сессионных настроек режимов. Единая точка для всех входов
        (свободный текст, inline /skill, /init)."""
        tid = uuid.uuid4().hex
        self.app.tui_state.active_task_id = tid
        self._active_task_id = tid
        # Открываем store сейчас, чтобы _track_pending мог писать meta.
        self._store = FileTaskStore(
            threads_dir=self.app._threads_dir or "",
            system=self.app._system,
        )
        # /new и автосоздание: новый тред наследует то, с чем работаем сейчас.
        self.app.persist_session_overrides()
        return tid

    def _prepare_for_new_run(self) -> None:
        """Подготовить TUI к новому запуску runner.start:
        сбросить error-флаг, очистить last_runner_error,
        сбросить meta.tui.status → running (B1: иначе после прошлого completed
        бейдж/список сессий врут, пока живой runner не перекрывает live).

        _mcp_status_row НЕ обнуляем: колонка — часть бренд-карточки (#brand-mcp),
        переживает пересборку (_clear_trace); ссылка остаётся валидной."""
        self._error_shown = False
        self._mcp_status_done = False
        self.app.last_runner_error = None
        self.app.last_terminal_outcome = None
        self.app.last_stop_requested = False
        self._stamp_tui_running()

    def _stamp_tui_running(self) -> None:
        """meta.tui.status=running + сброс last_indicator (новый ход, не итог прошлого)."""
        if self._store is None or self._active_task_id is None:
            return
        self._store.update_tui_meta(
            self._active_task_id,
            {"status": "running", "last_indicator": None},
        )

    def _update_mcp_indicator(self) -> None:
        if self._mcp_status_done:
            return
        progress = getattr(self.app, "_mcp_progress", None)
        if progress is None:
            return
        servers, _active = progress.snapshot()
        if not servers:
            return
        from tui.services.mcp_progress import format_mcp_progress, format_mcp_settle
        if self._mcp_status_row is None:
            return  # карточка ещё не смонтирована
        self._mcp_status_row.update(format_mcp_progress(servers))
        if progress.all_reported():
            self._mcp_status_done = True
            self.app._mcp_progress = None
            row = self._mcp_status_row
            mcp_line = format_mcp_settle(servers)
            self.set_timer(1.0, lambda: self._settle_mcp_row(row, mcp_line))

    def _settle_mcp_row(self, row, mcp_line: str) -> None:
        """Схлопнуть MCP-прогресс в итог-запись карточки: статус MCP + строка
        уведомлений + готовность. Считается ЗДЕСЬ, а не на boot: к моменту settle
        mcp_status осел → карточка и /notices согласованы (иначе счётчик уведомлений
        недосчитывал бы MCP-фейлы)."""
        try:
            self._refresh_notices_card(mcp_line=mcp_line)   # запоминает mcp_line для перерисовок
        except Exception:
            pass
        self._maybe_toast_warnings()       # notices финальны (mcp_status осел) → warning-тосты

    def _mcp_screen_snapshot(self):
        """Снапшот для экрана /mcp: предпочитаем boot-результат (app._mcp_snapshot,
        выставлен при сборке executor на старте), иначе — из meta активного треда."""
        snap = getattr(self.app, "_mcp_snapshot", None)
        if snap is not None:
            return snap
        if self._store is not None and self._active_task_id is not None:
            meta = self._store.get_task_meta(self._active_task_id)
            return (meta.get("runtime") or {}).get("mcp_tools")
        return None

    def _pending_wire_match(self, client_id: str, polled_content: str) -> bool:
        """Проверить, что pending-запись с этим client_id имеет wire_content,
        равный содержимому, пришедшему из polling-rendered .jsonl записи.

        Источник истины — in-memory кэш `_pending_wire_cache`; fallback —
        ``meta.tui.pending_user_messages[].wire_content`` (после сброса кэша).

        Сравнение по .strip(): polled-контент приходит из map_message уже
        обрезанным, а wire может иметь хвостовой whitespace (многострочный
        init-промпт из файла-шаблона кончается на \\n) — иначе оптимистичный
        маркер «/init» не схлопнулся бы и полный промпт тёк бы в трейс."""
        cached = self._pending_wire_cache.get(client_id)
        if cached is None:
            tid = self._active_task_id
            if tid is None or self._store is None:
                return False
            records = (self._store.get_task_meta(tid).get("tui") or {}).get(
                "pending_user_messages", []
            )
            for rec in records:
                if rec.get("client_id") == client_id:
                    cached = rec.get("wire_content") or ""
                    break
            else:
                return False
        return str(cached).strip() == polled_content.strip()

    def _pump_reasoning_stream_source(self) -> None:
        """Stream-think как источник reasoning-часов: пока sink в 'thinking' —
        источник 'stream' активен. start/stop идемпотентны → просто вызываем
        каждый тик по текущему состоянию сink."""
        sink = getattr(self.app, "_stream_sink", None)
        if sink is None:
            return
        r_state, _dur = sink.reasoning_state()
        if r_state == "thinking":
            self._reasoning_clock.start("stream")
        else:
            self._reasoning_clock.stop("stream")

    def _ui_frozen_for_select(self) -> bool:
        """F1: заморожен ли UI из-за app-select. Сигнал — Screen._selecting."""
        return bool(getattr(self, "_selecting", False))

    def _poll_active(self) -> None:
        # Потолок #trace = экран минус хром. Зависит только от стабильных высот
        # (header/indicator-bar[зарезервирован]/input/footer) → пересчёт синхронный и
        # без гонки (на гарде ставит max-height лишь при ресайзе / росте ввода).
        self._sync_trace_max_height()
        # Freeze UI на время app-select (§3.2 F2–F5). Пока Screen._selecting —
        # точечно глушим UI-мутации трейса (stick/scroll/stream paint/wipe),
        # но НЕ весь poll: footer/OSC/title/runner-логика/orphan/tail-события
        # без мутаций экрана продолжаются (F6 — runner жив, UI не тупеет целиком).
        _frozen = self._ui_frozen_for_select()
        if _frozen:
            if self._select_freeze_since is None:
                self._select_freeze_since = time.monotonic()
            # F5: timeout ~5с → снять выделение принудительно (runner не стопаем).
            if time.monotonic() - self._select_freeze_since > 5.0:
                try:
                    self.clear_selection()
                except Exception:
                    pass
                self._select_freeze_since = None
                self._selecting = False
                _frozen = False
        else:
            self._select_freeze_since = None
        # Липкость к низу: детект ручного скролла + повторное прижатие.
        # F2: при freeze — не обновляем stick и не прижимаем (текст стоит на месте).
        if not _frozen:
            self._update_stick_state()
            if self._stick_bottom:
                self._scroll_trace_end()
        # Отложенное удаление осиротевших реплик (cancel-with-undo), когда idle.
        self._run_orphan_cleanups()
        # Индикатор загрузки MCP — на старте (до появления активной задачи) тоже.
        self._update_mcp_indicator()
        self._pump_reasoning_stream_source()
        err = getattr(self.app, "last_runner_error", None)
        if err and not self._error_shown:
            self._mount_trace_row(
                prefix="[!] ", content=f"runtime: {err}",
                kind_class="line-error",
            )
            self._error_shown = True
            self._mark_pending_failed()
        # Инвариант: running tool-строки живут, только пока задача жива (live
        # runner / ask_human). Любой НЕ-live статус — стоп, обрыв runner'а, resume
        # брошенного треда или неизвестный исход (бейдж None) → осиротевшие вызовы
        # финализируются «прервано». Критерий «жива» = бейдж среди live-состояний.
        if self._active_calls and self._task_status_badge() not in ("[running]", "[asking]"):
            for row in self._active_calls.values():
                row.mark_interrupted()
            for widgets in self._nested_widgets_by_parent.values():
                for w in widgets:
                    mark = getattr(w, "mark_interrupted", None)
                    if callable(mark):
                        mark()
            self._active_calls.clear()
            self._nested_tool_rows.clear()
            self._nested_widgets_by_parent.clear()
            self._nested_anchor.clear()
            self._subagent_progress.reset()
        # ThinkingIndicator — пока runner работает, обычно виден внизу trace.
        # Эвристика «видимый ответ дан»: если последнее event это agent-текст
        # ИЛИ system-событие (think/policy — агент работает с текстом, не с
        # инструментами) и прошло ≥1.5с без новых событий → скрываем (hide-only,
        # не destroy). Если агент возьмёт инструмент (kind=call) — следующее
        # событие сбросит kind, индикатор вернётся с той же анимацией.
        _SOFT_HIDE_KINDS = {"agent", "system"}
        runner = getattr(self.app, "active_runner", None)
        is_running = runner is not None and runner.is_running()
        _kind = getattr(runner, "_thread_kind", "run") if runner is not None else "run"
        # Восходящий фронт: прогон стартовал → running (страховочная сетка к
        # _prepare_for_new_run; ловит любой путь, где prepare не вызвали).
        if (not self._was_running) and is_running and _kind != "compact":
            self._stamp_tui_running()
        # Спадающий фронт: агент мог создать скилл в прогоне → инвалидируем
        # TUI-кэш скиллов (/skill picker/inline); ленивый релоад при следующем
        # /skill. Открытие пикера/набор по клавишам диск не трогают.
        if self._was_running and not is_running:
            self._skills_cache = None
            # CTA про pending-диффы — после turn (не compact). Монтаж отложен
            # до конца poll / после stream_reset: иначе compaction стирает строку.
            # Правило B здесь НЕ проверяем: _events_since_last_user_send для
            # этого тика ещё может быть нулевым — хвост событий рендерится
            # позже в этом же _poll_active (_render_records ниже по методу).
            # Решение — в _flush_diff_pending_cta, там счётчик уже актуален.
            if _kind != "compact":
                self._diff_pending_cta_pending = "turn_end"
        # Спадающий фронт прогона True→False → зафиксировать терминальный статус
        # в meta.tui.status (TUI-owned), чтобы бейдж пережил переоткрытие сессии.
        if self._was_running and not is_running and self._store is not None \
                and self._active_task_id is not None \
                and _kind != "compact":
            outcome = resolve_terminal_outcome(
                runner_error=getattr(self.app, "last_runner_error", None),
                stop_requested=getattr(self.app, "last_stop_requested", False),
                terminal_outcome=getattr(self.app, "last_terminal_outcome", None),
            )
            # Прямой stop в обход _cmd_stop (active_runner НЕ обнулён) → не «completed».
            if outcome == "completed" and getattr(runner, "_stop_requested", False):
                outcome = "stopped"
            _STATUS = {"completed": "completed", "stopped": "stopped", "failed": "failed",
                       "stopped_steps": "stopped", "stopped_errors": "stopped",
                       "stopped_tokens": "stopped", "stopped_tool_calls": "stopped"}
            _KIND = {"completed": "done", "stopped": "cancelled", "failed": "failed",
                     "stopped_steps": "stopped_steps", "stopped_errors": "stopped_errors",
                     "stopped_tokens": "stopped_tokens", "stopped_tool_calls": "stopped_tool_calls"}
            _elapsed = None
            if outcome == "completed" and self._thinking_started_at is not None:
                _elapsed = int(time.monotonic() - self._thinking_started_at)
            self._store.update_tui_meta(
                self._active_task_id,
                {"status": _STATUS[outcome],
                 "last_indicator": {"kind": _KIND[outcome], "elapsed": _elapsed}},
            )
            self.app.last_stop_requested = False
            self._update_terminal_title()
        self._was_running = is_running
        # Активный ask_human — агент НЕ работает, он ждёт ответ юзера.
        # Индикатор активности здесь обманывает; банер ask_human уже
        # говорит «нужен ответ». Скрываем (не destroy — анимация
        # вернётся когда юзер ответит и агент продолжит).
        ask_human_active = (
            getattr(self.app, "tui_state", None) is not None
            and self.app.tui_state.ask_human_state is not None
        )
        # Тикающий elapsed на running tool-строках (running · Nс). Во время
        # человеко-ожидания (confirm / ask_human) агент заблокирован на решении
        # юзера — это не работа тула: замораживаем тикер и исключаем простой.
        human_wait = self._confirm_active or ask_human_active
        for row in self._active_calls.values():
            row.pause() if human_wait else row.resume()
            row.refresh_running_elapsed()
        # Nested progress субагента: drain runtime-очереди → строки под parent.
        self._drain_subagent_progress()
        summarizing = False
        finalizing = False
        finish_gate_checking = False
        _tmeta: dict = {}
        if self._store is not None and self._active_task_id is not None:
            try:
                _tmeta = self._store.get_task_meta(self._active_task_id)
                _rt = _tmeta.get("runtime") or {}
                summarizing = bool(_rt.get("summarizing"))
                finalizing = bool(_rt.get("finalizing"))
                finish_gate_checking = bool(_rt.get("finish_gate_checking"))
            except Exception:
                summarizing = False
                finalizing = False
                finish_gate_checking = False
                _tmeta = {}
        # Завершение сжатия (summarizing True→False) → baseline =
        # снимок meta.runtime.live_msg_count (persist уже пересчитал live_* на compact).
        if self._was_summarizing and not summarizing and self._store is not None \
                and self._active_task_id is not None:
            live_count = (_tmeta.get("runtime") or {}).get("live_msg_count")
            if live_count is not None:
                try:
                    self._store.update_tui_meta(
                        self._active_task_id,
                        {"last_compact_msg_count": int(live_count)},
                    )
                except (TypeError, ValueError):
                    pass
        self._was_summarizing = summarizing

        # Инвариант: нет активной задачи (пустое состояние) → никакого активного
        # индикатора. Терминальные исходы персистентны до следующего user-send —
        # не гасим их здесь (/_stop, Esc в ask_human).
        if self._active_task_id is None:
            ask_human_active = (
                getattr(self.app, "tui_state", None) is not None
                and self.app.tui_state.ask_human_state is not None
            )
            # При живом ask_human индикатор может быть смонтирован до появления
            # active_task_id (или после unmount task-switch), поэтому в этой
            # ветке не уничтожаем его, а даём _stop_for_ask_human перевести
            # в terminal-state (cancelled).
            if not ask_human_active:
                if (
                    self._thinking is None
                    or not is_terminal_indicator_state(self._thinking._state)
                ):
                    self._destroy_thinking_indicator()
            if not _frozen:
                self._clear_stream_block()
            self._refresh_footer()
            self._update_terminal_title()
            return

        self._session_lock_heartbeat()

        _indicator_mode = select_indicator_mode(
            err=err, is_running=is_running,
            ask_human=ask_human_active, confirm=self._confirm_active,
        )
        if _indicator_mode == "failed":
            # Краш раннера → ✗ Ошибка сразу и персистентно: не ждём
            # falling-edge/idle и не зависим от reaping воркера. _show создаёт
            # индикатор, если его нет.
            self._show_thinking_indicator(state="failed")
            if not _frozen:
                self._clear_stream_block()
        elif _indicator_mode == "active":
            import time as _time
            since_last = _time.monotonic() - self._last_event_at
            # summarizing → «сжимаю контекст…»; finish_gate_checking (судья turn_finish
            # реально сейчас вызывается) → «сверяюсь…» — ВЫШЕ защёлки _finalized, иначе
            # судья работает невидимо под «завершаю…»; finalizing (принят финальный ответ:
            # submit_solution ИЛИ финальная проза — идёт запись/саммари/flush/cleanup)
            # → «✓ завершаю…»; скрытое событие ≥1.5с (finish-gate сверяет / bounce /
            # доделка) → «сверяюсь…»; иначе «думаю…». Sentinel «_finalized» и флаг
            # tui_finalizing оба дают validating (см. select_active_substate).
            substate = select_active_substate(
                summarizing=summarizing,
                finalizing=finalizing,
                finish_gate_checking=finish_gate_checking,
                last_event_kind=self._last_event_kind,
                since_last=since_last,
                soft_hide_kinds=_SOFT_HIDE_KINDS,
            )
            self._show_thinking_indicator(state=substate)
            # Эфемерный StreamBlock — live-вывод текущего шага. Источник —
            # app._stream_sink (кормится из worker-потока через on_stream_delta);
            # монтаж/обновление строго здесь, на UI-потоке (set_interval-колбэк).
            # StreamBlock — только в активной фазе «thinking». В validating
            # (финальный ответ виден, идёт finish-gate) и compressing reasoning
            # не показываем — иначе строка-мысль висит после финального ответа (баг B).
            _thinking_phase = (not summarizing) and (not finalizing) and (self._last_event_kind != "_finalized")
            sink = getattr(self.app, "_stream_sink", None)
            # F3: при freeze не монтируем/не красим стрим-виджеты (mount/switch/
            # set_content дрожали бы экран под выделением). Контент догонит после MouseUp.
            if _thinking_phase and sink is not None and sink.has_content() and not _frozen:
                from tui.services.stream_sink import (
                    format_stream_block, format_reasoning_line, select_stream_render,
                )
                from tui.services.markdown_render import render_prose
                from tui.widgets.stream_block import StreamBlock
                think, answer = sink.snapshot()
                _cfg = getattr(self.app, "_config", None) or {}
                mode = _cfg.get("tui_stream_display", "minimal")
                clean = format_stream_block(answer=answer)
                reasoning = format_reasoning_line(think=think)
                kind, thinking = select_stream_render(mode, clean, reasoning)
                from tui.widgets.trace_row import TraceRow
                if kind == "answer":
                    if self._stream_block is not None:          # switch reasoning→answer
                        self._stream_block.remove()
                        self._stream_block = None
                    if self._stream_answer_row is None:
                        self._stream_answer_row = TraceRow(
                            prefix="● ", content=render_prose(clean),
                            kind_class="line-agent", focusable=False)
                        self._trace_mount(self._stream_answer_row, scroll=True)
                    self._refresh_stream_content()
                elif kind == "reasoning":
                    if self._stream_answer_row is not None:      # switch answer→reasoning
                        self._stream_answer_row.remove()
                        self._stream_answer_row = None
                    if self._stream_block is None:
                        self._stream_block = StreamBlock()
                        self._trace_mount(self._stream_block, scroll=True)
                    self._refresh_stream_content()
                else:
                    self._clear_stream_block(reset_sink=False)
            elif not _thinking_phase and not _frozen:
                self._clear_stream_block()
        elif _indicator_mode == "hide":
            # Агент заблокирован — ждёт ответа пользователя. Скрываем (не destroy),
            # анимация вернётся когда агент продолжит.
            self._hide_thinking_indicator()
        else:  # terminal
            # Idle. Терминальный исход прогона держим как строку-итог индикатора
            # (done → «✓ Выполнил за N», cancelled → «■ Отменено», failed →
            # «✗ Ошибка») — персистентно до следующего user-send (его _reset
            # уничтожит индикатор). Свежий/неизвестный исход → уничтожаем.
            li_kind = None
            status = None
            if self._store is not None and self._active_task_id is not None:
                tui = self._store.get_task_meta(self._active_task_id).get("tui") or {}
                li = tui.get("last_indicator")
                if isinstance(li, dict):
                    li_kind = li.get("kind")
                status = tui.get("status")
            term_state = resolve_live_terminal_indicator_state(
                last_indicator_kind=li_kind, status=status,
                stop_requested=getattr(runner, "_stop_requested", False),
            )
            if term_state is not None and self._thinking is not None:
                self._thinking.set_state(term_state)
            else:
                self._destroy_thinking_indicator()
            if not _frozen:
                self._clear_stream_block()
        # Footer-left зависит от is_running — обновляем каждый тик, чтобы
        # /stop появлялся/исчезал без отдельных вызовов из start/stop путей.
        self._refresh_footer()
        self._update_terminal_title()   # статус живой в табе: [running]→[completed] и т.д.
        if self._store is None or self._active_task_id is None or self._cursor is None:
            self._flush_diff_pending_cta()
            return
        tail = self._store.tail_events(self._active_task_id, self._cursor)
        # F3: при freeze НЕ перерисовываем трейс (wipe/clear_trace/render_records
        # дрожали бы экраном под выделением). Курсор двигаем, чтобы не копить
        # хвост — отрисовка догонит на следующем тике после MouseUp.
        if _frozen:
            self._cursor = tail.next_cursor
            return
        if tail.stream_reset:
            self._clear_trace()
            self._reset_render_state()
            records, cur = self._store.read_all_events(self._active_task_id)
            self._cursor = cur
            if records and self._store.is_compaction_summary(records[0]):
                self._render_segments(self._store.read_segments(self._active_task_id))
            else:
                self._render_records(records)
            self._merge_sidecar_nested(self._active_task_id)
            # После компакции (stream_reset) баннер мог пере-монтироваться для уже
            # отвеченного вопроса — снять, иначе ввод уйдёт в stdin «вникуда».
            self._reconcile_ask_human_after_render(allow_remnant=False)
            # I1/A: GC pending симметрично attach — delivered drop, orphan → failed.
            self._mount_failed_from_meta(self._active_task_id)
            self._refresh_header()
            self._refresh_footer()
            # Wipe стёр CTA: если idle и есть pending — показать снова. Правило B
            # тут ни при чём (не про итог хода, а про физическую потерю строки
            # при перерисовке трейса) — reason="wipe_restore" явно исключает
            # его в _flush_diff_pending_cta.
            if not is_running:
                self._diff_pending_cta_pending = "wipe_restore"
            self._flush_diff_pending_cta()
            return
        self._cursor = tail.next_cursor
        self._render_records(tail.new_records, live=True)
        self._refresh_header()
        self._flush_diff_pending_cta()

    # Ширина хрома вокруг колонки статусов. Порог узости считается по факту
    # (`_brand_fits_beside`), а не константой: длина строк статусов зависит от MCP и
    # уведомлений, и фиксированный порог либо рвал слова в колонке, либо уводил её
    # вниз раньше времени. Слагаемые (styles.tcss, поэтому и продублированы здесь,
    # как _MCP_COL_PAD): border round у #brand-block — 2, его padding 1 2 — 4,
    # padding 0 1 у #trace — 2, вертикальный скроллбар трейса — 1. Скроллбар
    # считаем всегда: в живой сессии он есть, а на пустом старте лишняя колонка
    # стоит одной пограничной ширины, где статусы встанут под логотипом — это
    # легальная раскладка, в отличие от «подк / люче / ны».
    _BRAND_CHROME = 9

    def _show_brand(self) -> None:
        if self._brand is not None and self._brand.parent is not None:
            return
        trace = self.query_one("#trace", VerticalScroll)
        # без класса line-meta: его margin-top:1 опускал бы MCP-блок ниже логотипа;
        # цвет MCP-колонки задаёт #brand-mcp (color: $deep-dim).
        self._mcp_status_row = Static("", id="brand-mcp", markup=False)
        try:
            wide = self._brand_fits_beside(self.app.size.width)
        except Exception:
            wide = True
        logo = Static(branding.logo_text(), classes="brand-logo")
        # Тэглайн — отдельной строкой под логотипом, центрирован по ширине лого
        # (а не border_subtitle: тот центрируется по всей рамке, не под брендом).
        tagline = Static(branding.tagline_under_logo(), classes="brand-tagline", markup=False)
        # Версия — своей строкой под тэглайном, с воздухом над ней (CSS): карточка это
        # визитка продукта, и номер читается там же, где имя. Рядом же появляется
        # «↑ доступна новая версия» — текущая версия и предложение обновиться
        # складываются в одну мысль.
        version = Static(branding.version_under_logo(), classes="brand-version", markup=False)
        left = Vertical(logo, tagline, version, id="brand-left")
        inner = Horizontal(left, self._mcp_status_row, id="brand-row")
        self._brand_row = inner          # раскладку ставит _apply_brand_layout ниже
        self._brand_wide = None
        brand = Vertical(inner, id="brand-block")
        trace.mount(brand)
        self._brand = brand
        self._apply_brand_layout(wide)

    def _brand_fits_beside(self, width: int) -> bool:
        """Влезает ли колонка статусов СБОКУ от логотипа целиком.

        Считаем по самой длинной строке статусов, а не по фиксированному порогу: их
        длина зависит от MCP и уведомлений, и любое постоянное число либо рвало слова
        («подк / люче / ны»), либо уводило колонку вниз там, где она помещалась."""
        from tui import branding
        lines = self._compose_card_lines(self._last_mcp_card_line).plain.splitlines()
        need = max((len(line) for line in lines), default=0)
        logo_w = max((len(line) for line in branding.LOGO.splitlines()), default=0)
        return width >= logo_w + _MCP_COL_PAD + need + self._BRAND_CHROME

    def _apply_brand_layout(self, wide: bool) -> None:
        """Статусы сбоку от логотипа (широкое окно) или под ним (узкое).

        Меняем layout уже смонтированного контейнера, а не пересобираем карточку:
        `remove()` у Textual отложенный, и на кадр в трейсе оказались бы две карточки.
        Под логотипом первой строке нужен воздух сверху, сбоку — нет."""
        row = self._brand_row
        if row is None or self._brand_wide == wide:
            return
        # Ссылку на контейнер держим сами: сразу после mount он ещё не находится
        # запросом (монтирование у Textual отложенное), а раскладку надо поставить
        # уже на сборке — иначе старт в узком окне остался бы в широкой раскладке.
        row.styles.layout = "horizontal" if wide else "vertical"
        if self._mcp_status_row is not None:
            toggle = (self._mcp_status_row.remove_class if wide
                      else self._mcp_status_row.add_class)
            toggle("brand-mcp-stacked")
        self._brand_wide = wide          # только после успешного применения

    def _collect_notices(self):
        """Бейдж КАРТОЧКИ: стартовые + unread runtime (без обновления — оно там строкой).

        Владелец сборки — App (_startup_notices / collect_unread_notices): экран не
        пересобирает то же самое, иначе новый источник уведомлений, добавленный на
        одной стороне, молча разъедется с другой."""
        collect = getattr(self.app, "collect_unread_notices", None)
        if callable(collect):
            return collect()
        return collect_unread_runtime_notices()

    def _collect_footer_notices(self):
        """Бейдж ФУТЕРА: то же + обновление (отдельной строки про него в футере нет)."""
        collect = getattr(self.app, "collect_footer_notices", None)
        if callable(collect):
            return collect()
        return self._collect_notices()

    def _has_mcp_servers(self) -> bool:
        """Заданы ли MCP-серверы в конфиге. При них mcp_status осядет только на
        MCP-settle — карточку досбираем тогда; без них — сразу на boot."""
        cfg = getattr(self.app, "_config", None) or {}
        return bool(cfg.get("mcp-servers"))

    def _compose_card_lines(self, mcp_line: "Optional[str]") -> Text:
        """Итог-содержимое правой колонки карточки: до четырёх строк через пустую строку.

            ⚠ MCP: 3/5 подключены · /mcp             (только если заданы MCP-серверы)

            ⚠2 ℹ1 · F4/notices                  (только если есть уведомления)

            ↑ доступна новая версия · /settings → Обновление  (teal, если авто-чек нашёл)

            ✓ готов к работе                         (всегда)

        Строка уведомлений — голый бейдж `format_notices_badge`, без слова-префикса:
        тот же вид, что в футере и на /sessions (единый источник — notices_ui).
        Детали уведомлений — на экране /notices (F4); трейс держим чистым."""
        notices = self._collect_notices()
        lines = []          # (text, style|None)
        if mcp_line:
            lines.append((mcp_line, None))
        badge = format_notices_badge(notices)
        if badge:
            lines.append((badge, None))                     # строка целиком — базовым цветом карточки
        if getattr(self.app, "_update_available", False):
            lines.append(("↑ доступна новая версия · /settings → Обновление", _ACCENT))
        lines.append(("✓ готов к работе", None))
        t = Text()
        for i, (s, style) in enumerate(lines):
            if i:
                t.append("\n\n")
            t.append(s, style=style)
        return t

    def refresh_update_notice(self) -> None:
        """Дорисовать строку «доступна новая версия» в brand-карточке — зовётся из
        async авто-чека (app) при находке обновления. MCP-строку карточки не трогаем:
        _refresh_notices_card без mcp_line переиспользует запомненную."""
        self._refresh_notices_card()

    def _maybe_toast_warnings(self) -> None:
        """Один раз при старте (когда notices финальны): warning-уведомления → тост,
        только если набор warning-кодов изменился с прошлого показа (on-change — не
        спамим тем же; карточка/notices держат постоянную видимость). info не тостим.
        Кап 3 + сводный «+N ещё». Персист набора — в agent_state.json."""
        if self._warnings_toasted:
            return
        self._warnings_toasted = True
        try:
            import upgrade_gate
            from startup_notices import warning_toasts
            messages, summary, codes = warning_toasts(self.app._startup_notices())
            last = frozenset(upgrade_gate.read_state().get("last_shown_warning_codes") or [])
            if codes == last:
                return                       # набор тот же — молчим (постоянная видимость в карточке)
            upgrade_gate.write_state({"last_shown_warning_codes": sorted(codes)})
            for msg in messages:
                self.app.notify(msg, severity="warning", timeout=6.0)
            if summary:
                self.app.notify(summary, severity="warning", timeout=6.0)
        except Exception:
            pass                             # тосты — best-effort, не роняют старт

    def _render_startup_notices(self) -> None:
        """Boot-оркестрация карточки. Без MCP-серверов данные полны сразу →
        выставляем карточку. С MCP-серверами откладываем до settle
        (см. _settle_mcp_row), где mcp_status уже осел."""
        if self._has_mcp_servers():
            return
        self._refresh_notices_card(mcp_line=None)
        self._maybe_toast_warnings()

    def _refresh_notices_card(self, *, mcp_line: "Optional[str]" = None) -> None:
        """Обновить правую колонку карточки: MCP + счётчик уведомлений + готовность."""
        if self._mcp_status_row is None:
            return
        if mcp_line is not None:
            self._last_mcp_card_line = mcp_line
        self._mcp_status_row.update(self._compose_card_lines(self._last_mcp_card_line))
        # Строки статусов только что могли стать длиннее (осели MCP, пришли
        # уведомления) — раскладку пересчитываем по НИМ, иначе колонка осталась бы
        # сбоку и рвала бы свежий текст по буквам.
        try:
            self._apply_brand_layout(self._brand_fits_beside(self.app.size.width))
        except Exception:
            pass

    # Шапка трейса: переживает пересборку (#brand-block — бренд,
    # MCP-статус живёт внутри карточки как #brand-mcp).
    _TRACE_HEADER_IDS = frozenset({"brand-block"})

    def _clear_trace(self) -> None:
        """Очистить #trace, сохранив шапку (инвариант: бренд — первый ребёнок).

        remove_children() в Textual асинхронный (отложенный prune), поэтому
        «снести всё + ремоунт» не работает: guard видит ещё живой parent,
        а немедленный ремоунт с тем же id дал бы DuplicateIds. Виджеты шапки
        просто не удаляем."""
        trace = self.query_one("#trace", VerticalScroll)
        trace.remove_children(
            [c for c in trace.children if c.id not in self._TRACE_HEADER_IDS]
        )
        self._show_brand()  # defensive: монтирует, только если шапки ещё нет

    @property
    def _any_picker_open(self) -> bool:
        """Открыт ли input-оверлей (палитра / skill / arg / file picker).

        Единственный источник истины для Esc-приоритета: сначала закрыть
        оверлей (их блоки в on_key), потом ask_human, потом Esc-state-machine.
        Новый оверлей — добавить сюда, guard'ы по веткам не дублировать."""
        return any(w is not None for w in (
            self._palette, self._skill_picker, self._arg_picker, self._file_picker,
        ))

    # R1: алиасы Ctrl+R для ручного переименования активной сессии.
    # Локальный маппинг (образец — diff.py _KEY_ALIASES); diff-модуль не тащим.
    # ctrl+к / ctrl+К — русская раскладка (клавиша «R» на той же физ. кнопке).
    _RENAME_KEYS = frozenset({"ctrl+r", "ctrl+R", "ctrl+к", "ctrl+К"})

    def on_key(self, event: events.Key) -> None:
        # R1: Ctrl+R (и алиасы раскладки) — переименование активной сессии.
        # Гейт _active_task_id + _store; до confirm-banner/pickers (как в спеке).
        if event.key in self._RENAME_KEYS \
                and self._active_task_id is not None \
                and self._store is not None:
            self._rename_active_session()
            event.stop()
            event.prevent_default()
            return

        # Confirm-баннер имеет наивысший приоритет: одиночный y/N перехватываем
        # до истории, Esc-лестницы и любых пикеров. TextArea скрыта, клавиши
        # не потребляются ею и доходят сюда.
        if self._confirm_active:
            key = event.key
            banner = self._confirm_banner
            if key == "up" and banner is not None:
                banner.move_selection(-1)
            elif key == "down" and banner is not None:
                banner.move_selection(1)
            elif key == "enter":
                from tui.widgets.confirm_banner import choice_for
                self._answer_confirm(choice_for(banner.selected) if banner is not None else "deny")
            elif key in ("y", "Y"):
                self._answer_confirm("once")
            elif key in ("s", "S"):
                self._answer_confirm("session")
            elif key in ("n", "N", "escape"):
                self._answer_confirm("deny")
            # иначе — проглотить без действия (нет случайного ввода)
            event.stop()
            event.prevent_default()
            return

        # Любая не-Esc клавиша сбрасывает pending hint (без прерывания обработки).
        if event.key != "escape" and self._esc_confirm_kind is not None:
            self._clear_footer_hint()

        # Голосовой ввод: Ctrl+T — toggle записи (не работает при открытых
        # пикерах и disabled-инпуте; во время transcribing toggle игнорируется).
        if event.key == "ctrl+t":
            event.stop()
            event.prevent_default()
            self._voice_toggle()
            return

        # Input history: ↑/↓ для навигации (smart line-position check).
        # ↑ → history prev только если курсор на верхней ВИЗУАЛЬНОЙ строке.
        # ↓ → history next только если курсор на нижней ВИЗУАЛЬНОЙ строке.
        # Визуальной, а не логической: длинная строка переносится (soft-wrap), и
        # логич. row 0/last не значит «курсор наверху/внизу экранно» — иначе ↑/↓
        # на середине переноса ложно уходит в историю вместо сдвига курсора.
        # Palette/picker имеют свои ↑/↓ ветки ниже, поэтому отключаем history-mode,
        # когда они открыты.
        # Гейт фокуса: история листается ТОЛЬКО когда инпут реально в фокусе.
        # Без него ↑/↓ всплывают в Screen.on_key от любого сфокусированного
        # виджета (Tab по трейсу) и ложно листают историю; в PyCharm-терминале
        # колесо мыши шлётся как ↑/↓ — гейт ограничивает его влияние полем ввода.
        if event.key == "up" and not self._any_picker_open:
            try:
                input_field = self.query_one("#cmdinput", CommandTextArea)
                if input_field.has_focus and input_field.cursor_on_first_visual_line():
                    self._history_prev()
                    event.stop()
                    event.prevent_default()
                    return
            except Exception:
                pass

        if event.key == "down" and not self._any_picker_open:
            try:
                input_field = self.query_one("#cmdinput", CommandTextArea)
                if input_field.has_focus and input_field.cursor_on_last_visual_line():
                    self._history_next()
                    event.stop()
                    event.prevent_default()
                    return
            except Exception:
                pass

        # Esc — единая лестница приоритетов: оверлей (Esc-блоки пикеров ниже) →
        # bang-shell cancel → 2-й-Esc-confirm → текст в инпуте (двухшаговая
        # очистка) → ask_human → running (undo-cancel / stop-hint) → выход.
        # Esc всегда разбирает самый локальный контекст; безусловный стоп —
        # /stop или Ctrl+C.
        if event.key == "escape" and len(self.app.screen_stack) == 1 \
                and not self._any_picker_open:
            # Запись голосом: Esc — отмена записи (самый локальный контекст).
            if self._voice is not None and self._voice.state == "recording":
                self._voice.cancel_recording()
                event.stop()
                event.prevent_default()
                return

            # Пока идёт `!` — cancel bang, не quit / не Esc-лестница выхода.
            if self._bang is not None and self._bang.is_running:
                self._bang.cancel()
                event.stop()
                event.prevent_default()
                return
            # 2-й Esc в окне 2s — выполнить отложенный action.
            if self._esc_confirm_kind is not None:
                kind = self._esc_confirm_kind
                self._clear_footer_hint()
                if kind == "clear":
                    try:
                        inp = self.query_one("#cmdinput", CommandTextArea)
                        inp.text = ""
                        inp._clear_chip_state()
                    except Exception:
                        pass
                elif kind == "stop":
                    self._cmd_stop()
                event.stop()
                event.prevent_default()
                return

            input_text = ""
            try:
                input_text = self.query_one("#cmdinput").text
            except Exception:
                pass

            # Текст в инпуте → сначала двухшаговая очистка, при любом состоянии
            # (running / ask_human): расплата за случайный Esc — набранный
            # текст, а не убитый run или отменённый вопрос.
            if input_text:
                self._show_esc_hint("clear")
                event.stop()
                event.prevent_default()
                return

            # Пустое поле + live ask_human → clean stop треда (а не пустой ответ
            # агенту): юзер ожидает «выйти из вопроса», агент в петлю ask_human
            # не зацикливается. Zombie ask_human (no live runner) — снимаем
            # banner и продолжаем лестницу ниже (stop/exit).
            if self.app.tui_state.ask_human_state is not None:
                runner = getattr(self.app, "active_runner", None)
                if runner is not None and runner.is_running():
                    self._stop_for_ask_human()
                    event.stop()
                    return
                self._unmount_ask_human_banner()

            runner = getattr(self.app, "active_runner", None)
            is_running = runner is not None and runner.is_running()
            if is_running:
                # Cancel-with-undo case: 0 событий после отправки → сразу, без hint
                # (пустота инпута гарантирована веткой очистки выше).
                if self._events_since_last_user_send == 0:
                    self._cancel_with_undo()
                    event.stop()
                    event.prevent_default()
                    return
                # Иначе — показать stop hint.
                self._show_esc_hint("stop")
                event.stop()
                event.prevent_default()
                return

            # Пусто, run не идёт → модалка выхода.
            self._request_exit()
            event.stop()
            event.prevent_default()
            return

        # Skill picker имеет приоритет над командной палитрой (mutex — но защитимся).
        if self._skill_picker is not None:
            if event.key == "escape":
                self._close_skill_picker()
                event.stop()
                event.prevent_default()
                return
            if event.key in ("up", "down"):
                self._skill_picker.move_selection(-1 if event.key == "up" else 1)
                event.stop()
                event.prevent_default()
                return
            if event.key == "tab":
                skill = self._skill_picker.selected_skill()
                if skill is not None:
                    inp = self.query_one("#cmdinput", CommandTextArea)
                    parts = inp.text.split(None, 1)
                    first = parts[0] if parts else "/skill"
                    self._suppress_picker_reopen = True
                    inp.text = f"{first} {skill['id']}"
                    inp.move_cursor(inp.document.end)
                    self._close_skill_picker()
                event.stop()
                event.prevent_default()
                return
            # Enter обрабатывается в on_command_text_area_submitted
            return

        # Arg picker (выбор значения аргумента, напр. /export txt|json) —
        # тот же key-контракт, что у skill picker.
        if self._arg_picker is not None:
            if event.key == "escape":
                self._close_arg_picker()
                event.stop()
                event.prevent_default()
                return
            if event.key in ("up", "down"):
                self._arg_picker.move_selection(-1 if event.key == "up" else 1)
                event.stop()
                event.prevent_default()
                return
            if event.key == "tab":
                value = self._arg_picker.selected_value()
                if value is not None:
                    inp = self.query_one("#cmdinput", CommandTextArea)
                    parts = inp.text.split(None, 1)
                    first = parts[0] if parts else "/export"
                    self._suppress_picker_reopen = True
                    inp.text = f"{first} {value}"
                    inp.move_cursor(inp.document.end)
                    self._close_arg_picker()
                event.stop()
                event.prevent_default()
                return
            # Enter обрабатывается в on_command_text_area_submitted
            return

        # File picker (@-аттач) — тот же key-контракт, что у skill/arg picker.
        if self._file_picker is not None:
            if event.key == "escape":
                self._close_file_picker()
                event.stop()
                event.prevent_default()
                return
            if event.key in ("up", "down"):
                self._file_picker.move_selection(-1 if event.key == "up" else 1)
                event.stop()
                event.prevent_default()
                return
            if event.key == "tab":
                self._complete_file_picker()
                event.stop()
                event.prevent_default()
                return
            # Enter обрабатывается в on_command_text_area_submitted
            return

        if self._palette is None:
            return
        if event.key == "escape":
            self._close_palette()
            inp = self.query_one("#cmdinput", CommandTextArea)
            inp.text = ""
            event.stop()
            event.prevent_default()
        elif event.key in ("up", "down"):
            self._palette.move_selection(-1 if event.key == "up" else 1)
            event.stop()
            event.prevent_default()
        elif event.key in ("left", "right"):
            cmd = self._palette.selected_command()
            if cmd is not None and cmd.is_toggle:
                spec = TOGGLES.get(cmd.name)
                if spec is not None and spec.session_switch_profile:
                    # /profiles — пикер, не цикл (спека §12.1, ревизия 2026-07-28):
                    # список произвольной длины не листается вслепую.
                    self._open_profiles_picker(cmd.name)
                    event.stop()
                    event.prevent_default()
                    return
                # Мгновенная модель (спека §1): ←/→ применяет в сессию сразу.
                # custom не выбирается (ревизия 4) — /rigor крутит только
                # low/medium/high, ровно как Settings.
                d = -1 if event.key == "left" else 1
                new_value = next_toggle_value(self.app, cmd.name, d)
                try:
                    apply_toggle(self.app, cmd.name, new_value)
                except (ValueError, OSError) as exc:
                    self._show_footer_hint(f"✗ не сохранено: {exc}")
                else:
                    self._palette.refresh_toggle_state()
                event.stop()
                event.prevent_default()
            # иначе — обычная строка: не перехватываем, ←/→ уходит в TextArea (каретка)
        elif event.key == "tab":
            cmd = self._palette.selected_command()
            if cmd is not None:
                inp = self.query_one("#cmdinput", CommandTextArea)
                inp.text = f"/{cmd.name} "
                inp.move_cursor(inp.document.end)
                self._close_palette()
            event.stop()
            event.prevent_default()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "cmdinput":
            return
        # Печать в input после 1-го Esc сбрасывает pending hint — клавиша уже
        # «не Esc», подтверждать нечего. Screen.on_key для печатных не вызывается
        # (TextArea их потребляет), поэтому ловим здесь.
        if self._esc_confirm_kind is not None:
            self._clear_footer_hint()
        # Сброс history-navigation state при user-правке input.
        # Программные правки из _history_prev/_next запоминают последний text;
        # если current совпадает — это Changed от навигации, не сбрасываем.
        if self._history_index is not None:
            current_text = event.text_area.text
            if current_text != self._history_last_programmatic_text:
                self._reset_history_navigation()
        # Для палитры/пикера важна только первая строка (команды без переносов).
        val = event.text_area.text.split("\n", 1)[0]

        # Состояние 1: /<prefix> без пробела → командная палитра.
        if val.startswith("/") and " " not in val:
            if self._skill_picker is not None:
                self._close_skill_picker()
            if self._arg_picker is not None:
                self._close_arg_picker()
            if self._file_picker is not None:
                self._close_file_picker()
            if self._palette is None:
                self._open_palette(prefix=val[1:])
            else:
                self._palette.set_filter(val[1:])
            return

        # Состояние 1b: ровно `.` → та же палитра (клавиша `/` в RU-раскладке).
        # Фильтр по дальнейшему вводу не делаем: латиница команд в RU недоступна,
        # выбор — стрелками/Enter/Tab; любой символ после `.` палитру закрывает.
        if val == ".":
            if self._skill_picker is not None:
                self._close_skill_picker()
            if self._arg_picker is not None:
                self._close_arg_picker()
            if self._file_picker is not None:
                self._close_file_picker()
            if self._palette is None:
                self._open_palette(prefix="")
            else:
                self._palette.set_filter("")
            return

        # Состояние 2: /skill <фильтр> → skill-picker.
        if val.startswith("/") and " " in val:
            first, _, rest = val.partition(" ")
            cmd = first[1:]
            if cmd == "skill":
                # /skill <валидный_id> <текст> → скилл зафиксирован, дальше промпт:
                # закрываем picker (его фильтр по 'id текст' бессмыслен).
                if self._skills_cache is None:
                    self._skills_cache = self._load_skills_safe()
                skill_first, _, skill_rest = rest.partition(" ")
                if skill_rest.strip() and any(
                    str(s.get("id")) == skill_first for s in self._skills_cache
                ):
                    if self._skill_picker is not None:
                        self._close_skill_picker()
                    return
                if self._suppress_picker_reopen:
                    self._suppress_picker_reopen = False
                    return
                if self._palette is not None:
                    self._close_palette()
                if self._skill_picker is None:
                    self._open_skill_picker(prefix=rest)
                else:
                    self._skill_picker.set_filter(rest)
                return

            # Состояние 3: /<cmd> <фильтр>, где у команды есть arg_options
            # (статические или динамические через arg_options_provider) → arg-picker.
            cmd_obj = self._registry.lookup(cmd)
            if cmd_obj is not None and (cmd_obj.arg_options
                                        or cmd_obj.arg_options_provider is not None):
                if self._suppress_picker_reopen:
                    self._suppress_picker_reopen = False
                    return
                if self._palette is not None:
                    self._close_palette()
                if self._skill_picker is not None:
                    self._close_skill_picker()
                if self._arg_picker is None:
                    options = (list(cmd_obj.arg_options) if cmd_obj.arg_options
                               else list(cmd_obj.arg_options_provider(self.app)))
                    hint_provider = None
                    if cmd_obj.arg_hint_provider is not None:
                        provider = cmd_obj.arg_hint_provider
                        hint_provider = lambda value: provider(self.app, value)
                    self._open_arg_picker(options=options, prefix=rest,
                                          hint_provider=hint_provider)
                else:
                    self._arg_picker.set_filter(rest)
                return

            # Состояние 3b: /<cmd> <query>, команда с файловым аргументом
            # (Command.arg_file) → файловый пикер (тот же, что у @-сегмента).
            # Стоит ДО @-детекта: '@' внутри такой строки — просто текст фильтра.
            if cmd_obj is not None and cmd_obj.arg_file:
                if self._suppress_picker_reopen:
                    self._suppress_picker_reopen = False
                    return
                if self._palette is not None:
                    self._close_palette()
                if self._skill_picker is not None:
                    self._close_skill_picker()
                if self._arg_picker is not None:
                    self._close_arg_picker()
                self._show_file_picker(query=rest)
                return

        # Состояние 4: активный @-сегмент под курсором → file-picker.
        inp = event.text_area
        try:
            row, col = inp.cursor_location
            line = inp.text.split("\n")[row]
        except Exception:
            line, col = val, len(val)
        from tui.services.file_index import parse_at_segment
        seg = parse_at_segment(line, col)
        if seg is not None:
            if self._palette is not None:
                self._close_palette()
            if self._skill_picker is not None:
                self._close_skill_picker()
            if self._arg_picker is not None:
                self._close_arg_picker()
            _, query = seg
            self._show_file_picker(query=query)
            return
        if self._file_picker is not None:
            self._close_file_picker()

        # Иначе: все закрыты.
        if self._palette is not None:
            self._close_palette()
        if self._skill_picker is not None:
            self._close_skill_picker()
        if self._arg_picker is not None:
            self._close_arg_picker()

    _MIN_TRACE_WITH_OVERLAY = 3   # минимум строк трейса видно при открытом оверлее

    def _open_overlay_widget(self):
        """Открытый input-оверлей (палитра/skill/arg/file-picker) или None."""
        return (self._palette or self._skill_picker
                or self._arg_picker or self._file_picker)

    def _mount_input_overlay(self, widget) -> None:
        """Смонтировать input-оверлей (палитра / skill/arg/file-picker) СРАЗУ ПОД
        полем ввода — список раскрывается ВНИЗ (новая раскладка: ввод следует за
        контентом, под ним есть место). Потолок оверлея и ужатие #trace под его
        желаемую высоту считает _sync_trace_max_height (по virtual_size оверлея);
        отложенный вызов даёт это сразу, не дожидаясь poll-тика."""
        self.query_one("#root", Vertical).mount(
            widget, after=self.query_one("#input-row", Horizontal)
        )
        self.call_after_refresh(self._sync_trace_max_height)

    def _open_palette(self, *, prefix: str) -> None:
        if self._palette is not None:
            return
        palette = CommandPalette(
            self._registry, initial_filter=prefix,
            toggle_state=lambda name: toggle_state(self.app, name),
            toggle_extra=lambda name: preset_track_extra(self.app, name),
            toggle_custom_hint=lambda name: preset_custom_hint(self.app, name),
            toggle_dynamic_hint=lambda name, value: dynamic_toggle_hint(self.app, name, value),
        )
        self._palette = palette
        self._mount_input_overlay(palette)

    def _close_palette(self) -> None:
        if self._palette is None:
            return
        self._palette.remove()
        self._palette = None

    def _open_skill_picker(self, *, prefix: str) -> None:
        if self._skill_picker is not None:
            return
        if self._skills_cache is None:
            self._skills_cache = self._load_skills_safe()
        from tui.widgets.skill_picker import SkillPicker
        picker = SkillPicker(self._skills_cache, initial_filter=prefix)
        self._skill_picker = picker
        self._mount_input_overlay(picker)

    def _close_skill_picker(self) -> None:
        if self._skill_picker is None:
            return
        self._skill_picker.remove()
        self._skill_picker = None

    def _open_arg_picker(self, *, options: list[tuple[str, str]], prefix: str,
                         hint_provider=None) -> None:
        if self._arg_picker is not None:
            return
        from tui.widgets.arg_picker import ArgPicker
        picker = ArgPicker(options, initial_filter=prefix, hint_provider=hint_provider)
        self._arg_picker = picker
        self._mount_input_overlay(picker)

    def _open_profiles_picker(self, name: str = "profiles") -> None:
        """Раскрыть пикер /profiles (спека §12.1, ревизия 2026-07-28): текст
        `/profiles ` в input → on_text_area_changed (состояние 3) сам откроет
        ArgPicker с динамическими опциями и хинтом состава слотов."""
        inp = self.query_one("#cmdinput", CommandTextArea)
        if self._palette is not None:
            self._close_palette()
        inp.text = f"/{name} "
        inp.move_cursor(inp.document.end)

    def _close_arg_picker(self) -> None:
        if self._arg_picker is None:
            return
        self._arg_picker.remove()
        self._arg_picker = None

    def _project_root(self) -> str:
        """Корень обзора @-аттача = текущий cwd проекта (корень FS-тулов агента)."""
        return self._current_cwd()

    def _file_picker_entries(self, query: str) -> list:
        """Entries для file-picker: query с хвостовым '/' — листинг каталога,
        иначе fuzzy-поиск по корню проекта."""
        from tui.services.file_index import fuzzy_search, list_dir
        root = self._project_root()
        return (list_dir(root, query[:-1]) if query.endswith("/")
                else fuzzy_search(root, query))

    def _show_file_picker(self, *, query: str) -> None:
        """Открыть/обновить файловый пикер; совпадений нет → скрыть.

        Пустой ящик «(нет совпадений)» не показываем: выбрать из него нечего,
        а информация «не нашлось» видна по отсутствию списка. Пикер сам
        возвращается, как только fuzzy снова что-то находит (состояния
        пересчитываются на каждый Changed)."""
        entries = self._file_picker_entries(query)
        if not entries:
            if self._file_picker is not None:
                self._close_file_picker()
            return
        if self._file_picker is None:
            from tui.widgets.file_picker import FilePicker
            picker = FilePicker(entries)
            self._file_picker = picker
            self._mount_input_overlay(picker)
        else:
            self._file_picker.set_entries(entries)

    def _close_file_picker(self) -> None:
        if self._file_picker is None:
            return
        self._file_picker.remove()
        self._file_picker = None

    def _complete_file_picker(self) -> None:
        """Достроить выбранный путь в открытом файловом пикере — два контекста,
        приоритет как в state-цепочке on_text_area_changed (state 3b раньше
        @-детекта, см. state 4): сначала файловый аргумент команды
        (/<cmd с arg_file> <query> → путь без '@'), иначе — активный
        @-сегмент под курсором (→ '@path'). Папка → drill-in (пикер
        остаётся); файл → путь (+' ' у @-ветки) и закрыть."""
        if self._file_picker is None:
            return
        entry = self._file_picker.selected_entry()
        if entry is None:
            return
        inp = self.query_one("#cmdinput", CommandTextArea)
        row, col = inp.cursor_location
        lines = inp.text.split("\n")
        line = lines[row]

        first, sep, _rest = line.partition(" ")
        cmd_obj = (self._registry.lookup(first[1:])
                   if sep and first.startswith("/") else None)
        if cmd_obj is not None and cmd_obj.arg_file:
            start = len(first) + 1
            if col < start:
                # Курсор левее зоны аргумента (клик в имя команды) —
                # достройка здесь исказила бы строку, no-op.
                return
            is_dir = entry.endswith("/")
            replacement = entry + ("" if is_dir else " ")
            new_line = line[:start] + replacement + line[col:]
            if not is_dir:
                # Достроенная строка всё ещё матчит state 3b (/<cmd> <query>) —
                # без suppress следующий Changed тут же переоткроет пикер.
                self._suppress_picker_reopen = True
            lines[row] = new_line
            inp.text = "\n".join(lines)
            inp.cursor_location = (row, start + len(replacement))
            if is_dir:
                self._show_file_picker(query=entry)
            else:
                self._close_file_picker()
            return

        from tui.services.file_index import parse_at_segment
        seg = parse_at_segment(line, col)
        if seg is None:
            return
        start, _query = seg
        is_dir = entry.endswith("/")
        replacement = "@" + entry + ("" if is_dir else " ")
        new_line = line[:start] + replacement + line[col:]
        lines[row] = new_line
        inp.text = "\n".join(lines)
        inp.cursor_location = (row, start + len(replacement))
        if is_dir:
            self._show_file_picker(query=entry)
        else:
            self._close_file_picker()

    def on_command_text_area_submitted(
        self, event: CommandTextArea.Submitted
    ) -> None:
        inp = self.query_one("#cmdinput", CommandTextArea)
        # Запись голосом: Enter = стоп+распознать (НЕ отправка сообщения).
        if self._voice is not None and self._voice.state == "recording":
            self._voice.stop_and_transcribe()
            return

        # Skill picker открыт → выполнить выбранный skill (или ввод как есть)
        if self._skill_picker is not None:
            skill = self._skill_picker.selected_skill()
            self._close_skill_picker()
            text = event.value.strip()
            inp.text = ""
            if skill is not None and text:
                parts = text.split(None, 1)
                first = parts[0] if parts else "/skill"
                cmd_name = first[1:] if first.startswith("/") else first
                self._handle_command(f"{cmd_name} {skill['id']}")
            elif text.startswith("/"):
                # Пустой результат фильтра — выполнить ввод как обычную команду.
                self._handle_command(text[1:])
            return

        # File picker открыт → Enter достраивает путь (не сабмитит сообщение).
        if self._file_picker is not None:
            if self._file_picker.selected_entry() is None:
                # Fuzzy ничего не нашёл (например, абсолютный/внешний путь) —
                # закрыть пикер и пропустить Enter в обычный submit-путь.
                self._close_file_picker()
            else:
                self._complete_file_picker()
                return

        # Arg picker открыт → Enter выполняет команду с выбранным/набранным значением.
        if self._arg_picker is not None:
            value = self._arg_picker.selected_value()
            self._close_arg_picker()
            typed = event.value.strip()
            inp.text = ""
            if typed.startswith("/"):
                parts = typed.split(None, 1)
                cmd_name = parts[0][1:]
                typed_arg = parts[1].strip() if len(parts) > 1 else ""
                arg = value if value is not None else typed_arg
                self._handle_command(f"{cmd_name} {arg}".strip())
            return

        # Если открыта палитра — Enter выполняет выбранную/набранную команду
        # (логика перенесена из on_key, т.к. TextArea перехватывает Enter).
        # Команды со схемой args (`<path>`, `[txt|…]`) подставляются в поле;
        # выполнение (с дефолтом, если args опциональны) — следующим Enter в поле.
        if self._palette is not None:
            cmd = self._palette.selected_command()
            if cmd is not None and cmd.is_toggle:
                spec = TOGGLES.get(cmd.name)
                if spec is not None and spec.session_switch_profile:
                    # /profiles: Enter раскрывает пикер (спека §12.1, ревизия).
                    self._open_profiles_picker(cmd.name)
                    return
                # Toggle-строка: Enter не сабмитит текст — циклит (+1) и
                # применяет мгновенно, тот же путь, что ←/→ (спека 2026-07-18
                # §1). custom не выбирается (ревизия 4) — тот же роутинг, что
                # и ←/→ выше. Палитра остаётся открытой.
                new_value = next_toggle_value(self.app, cmd.name, 1)
                try:
                    apply_toggle(self.app, cmd.name, new_value)
                except (ValueError, OSError) as exc:
                    self._show_footer_hint(f"✗ не сохранено: {exc}")
                else:
                    self._palette.refresh_toggle_state()
                return
            typed = event.value.strip()
            has_args = len(typed.split()) > 1
            if cmd is not None and not has_args:
                if cmd.args:
                    inp.text = f"/{cmd.name} "
                    inp.move_cursor(inp.document.end)
                    self._close_palette()
                    inp.focus()
                    return
                self._close_palette()
                inp.text = ""
                self._handle_command(cmd.name)
                return
            # Свободный ввод с / — выполнить (например, /view path/to/file)
            if typed.startswith("/"):
                self._close_palette()
                inp.text = ""
                self._handle_command(typed[1:])
                return
            # Всё остальное — закрыть палитру и очистить ввод
            self._close_palette()
            inp.text = ""
            return

        text = event.value.strip()
        inp.text = ""
        if not text:
            return

        # Ask-human mode: input is the answer to the agent's question.
        if self.app.tui_state.ask_human_state is not None:
            target = self._ask_human_target_runner()
            if target is not None:
                # Живой in-run диалог: владелец вопроса тот же и работает → stdin.
                child_pid = self._ask_human_child_parent_call_id
                if child_pid:
                    # Child HITL: ответ только в stdin субагента. Parent pending /
                    # user-строка в data_* — нет (иначе orphan в meta + «уехало к агенту»).
                    self._mount_nested_user(parent_call_id=child_pid, prose=text)
                    parent_row = self._active_calls.get(child_pid)
                    if parent_row is not None:
                        parent_row.resume()
                else:
                    client_id = self._track_pending(
                        display_text=text, wire_content=text
                    )
                    self._mount_user_reply(
                        text, state="pending", client_id=client_id
                    )
                target.submit_ask_human_reply(text)
                # Optimistic: снимаем баннер сразу после ответа, не ждём
                # function_call_output в полле (иначе 5–10с «висящего» вопроса
                # и hide индикатора, пока агент уже работает). Cleanup B-2
                # в _render_event остаётся страховкой для replay/cancel.
                self._unmount_ask_human_banner()
                self._reset_thinking_timer()
                self._on_user_text_submitted(text)
                self._show_thinking_indicator(state="thinking")
                return
            # Владельца нет / сменился / завис → отвечаем новым ходом.
            self._unmount_ask_human_banner()
            runner = self.app.active_runner
            if runner is not None and runner.is_running():
                # Чужой/зависший раннер не читает этот вопрос — кооперативно стопаем,
                # чтобы get_runner ниже создал свежий и start не отбился.
                self._stop_and_replace_runner()
            # fall-through ↓ к "continue thread": тот же active_task_id, start(answer).

        if text.startswith("/"):
            self._handle_command(text[1:])
            return

        if text.startswith("!"):
            self._handle_bang_shell(text)
            return

        runner = self.app.get_runner()

        # id user-сообщения этого хода: пробрасываем в run, чтобы при быстрой
        # отмене (до вывода агента) точечно удалить осиротевшую реплику из треда.
        msg_id = "m_" + uuid.uuid4().hex
        self._last_user_message_id = msg_id

        if self.app.tui_state.active_task_id is None:
            # AUTO-CREATE: первый свободный текст → новая задача.
            # Применяем pre-stage: pending_skill_id встраиваем в первое
            # сообщение и фиксируем в meta.tui.
            tid = self._begin_auto_created_task()
            full_query, patch = self._consume_pending_prefix(text)
            display_query, skill_loaded = self._skill_display_args(text)
            technical = self._build_attachment_technical(text)
            self._prepare_for_new_run()
            self._reset_thinking_timer()
            self._on_user_text_submitted(text)
            runner.start(query=full_query, thread_id=tid, system=self.app._system,
                         message_id=msg_id, technical=technical,
                         display_query=display_query, skill_loaded=skill_loaded)
            self.attach_active_task(tid)
            if patch:
                self._store.update_tui_meta(tid, patch)
            branch = current_branch(self._current_cwd())
            if branch:
                self._store.update_tui_meta(tid, {"branch": branch})
            # Абсолютный путь проекта на момент создания сессии (исторический,
            # не живой cwd). Безусловно — путь есть даже в не-git папке.
            try:
                ppath = str(Path(self._current_cwd()).resolve())
            except Exception:
                ppath = ""
            if ppath:
                self._store.update_tui_meta(tid, {"project_path": ppath})
            # _track_pending — после attach_active_task (которое чистит
            # _pending_user_widgets через _reset_render_state) и после
            # потенциальной первой записи meta исполнителем.
            client_id = self._track_pending(display_text=text, wire_content=full_query)
            self._mount_user_reply(text, state="pending", client_id=client_id)
            return

        if runner.is_running():
            from tools.subagents.active_scope import is_child_input_active

            self._reset_thinking_timer()
            self._on_user_text_submitted(text)
            # Active subagent → nested under subagent_* (progress/sidecar); иначе
            # parent pending + обычная user-строка в трейсе.
            if not is_child_input_active():
                client_id = self._track_pending(display_text=text, wire_content=text)
                self._mount_user_reply(text, state="pending", client_id=client_id)
            runner.add_message({"role": "user", "content": text, "id": msg_id})
            technical = self._build_attachment_technical(text)
            if technical is not None:
                runner.add_message({"role": "user", "content": technical})
            return

        # Задача есть, но run завершился (или открыта через /sessions) — продолжаем тот же
        # thread, применяя отложенные skill/cwd к этому сообщению.
        full_query, patch = self._consume_pending_prefix(text)
        display_query, skill_loaded = self._skill_display_args(text)
        if patch and self._store is not None:
            self._store.update_tui_meta(self.app.tui_state.active_task_id, patch)
        client_id = self._track_pending(display_text=text, wire_content=full_query)
        self._mount_user_reply(text, state="pending", client_id=client_id)
        self._prepare_for_new_run()
        self._reset_thinking_timer()
        self._on_user_text_submitted(text)
        technical = self._build_attachment_technical(text)
        runner.start(query=full_query, thread_id=self.app.tui_state.active_task_id,
                     system=self.app._system, message_id=msg_id, technical=technical,
                     display_query=display_query, skill_loaded=skill_loaded)

    def on_command_text_area_paste_collapsed(
        self, event: CommandTextArea.PasteCollapsed
    ) -> None:
        self._show_footer_hint(_PASTE_HINT_TEXT, seconds=5.0)

    def _consume_pending_prefix(self, text: str) -> tuple[str, dict]:
        """Встроить отложенный skill в сообщение; вернуть (query, meta-patch).

        Сбрасывает pending_skill_id. Используется и при auto-create,
        и при продолжении существующего thread'а.
        """
        pending_skill = self.app.tui_state.pending_skill_id
        self.app.tui_state.pending_skill_id = None
        prefix: list[str] = []
        patch: dict = {}
        if pending_skill:
            from tools.skills.registry import get_skills_registry
            from tools.skills.skill_delivery import build_skill_delivery, skill_is_available
            reg = get_skills_registry(getattr(self.app, "_config", None) or {})
            prefix.append(build_skill_delivery(pending_skill, reg))
            patch["skills"] = [pending_skill]
            self._pending_skill_marker = (pending_skill, skill_is_available(pending_skill, reg))
        return ("\n".join(prefix + [text]) if prefix else text), patch

    def _skill_display_args(self, clean_text: str) -> tuple[str | None, str | None]:
        """(display_query, skill_loaded) для runner.start по итогу _consume_pending_prefix.

        display_query = чистый текст, только если был инжект скилла;
        skill_loaded = id, если скилл загружен; иначе оба None (обычное сообщение).
        """
        marker = self._pending_skill_marker
        if not marker:
            return None, None
        skill_id, loaded = marker
        return clean_text, (skill_id if loaded else None)

    def _build_attachment_technical(self, text: str) -> Optional[str]:
        """Технический футер для @-вложений в тексте (резолв на сабмите).
        None — если в тексте нет существующих @-путей."""
        from tui.services.file_index import (
            extract_at_mentions, resolve_attachment, build_attachment_footer,
        )
        from message_policy.user_message_policy import wrap_technical_user_content
        root = self._project_root()
        resolved = []
        for rel in extract_at_mentions(text):
            r = resolve_attachment(root, rel)
            if r is not None:
                resolved.append(r)
        body = build_attachment_footer(resolved)
        if not body:
            return None
        return wrap_technical_user_content(body, technical_type="technical")

    def _cmd_settings(self) -> None:
        from tui.screens.settings import SettingsScreen
        self.app.reload_config_if_changed()   # свежесть глобала — только на пути Settings (дизайн 2026-07-21 §4)
        self.app.push_screen(SettingsScreen(
            config=self.app.screen_config(),
            config_path=getattr(self.app, "_config_path", None),
            on_apply=self.app.reload_config,
        ))

    def _cmd_config(self) -> None:
        from tui.screens.config import ConfigScreen
        ref = self.app.active_profile_ref()
        profile_source = None
        profile_path = None
        if ref is not None:
            profile_source = "проектный" if ref.root_kind == "project" else "глобальный"
            profile_path = str(ref.path)
        self.app.push_screen(ConfigScreen(
            config=self.app.effective_config(),
            config_path=getattr(self.app, "_config_path", None),
            project_local=getattr(self.app, "_config_project_local", None),
            profile_source=profile_source,
            profile_path=profile_path,
        ))

    def _cmd_view(self, arg: str) -> None:
        """`/view <path>` → FileViewerScreen (ошибки тоже на этом экране)."""
        from tui.services.file_view import FileViewSettings
        from tui.screens.file_view import FileViewerScreen

        cfg = self.app.screen_config()
        store = getattr(self.app, "view_positions", None)
        if store is None:
            from tui.services.file_view import ViewPositionStore
            store = ViewPositionStore()
            self.app.view_positions = store
        self.app.push_screen(FileViewerScreen(
            raw_path=arg or "",
            cwd=self._current_cwd(),
            store=store,
            settings=FileViewSettings.from_mapping(cfg.get("tui_file_view")),
        ))

    def _cmd_mcp(self) -> None:
        from tui.screens.mcp import McpScreen
        self.app.push_screen(McpScreen(
            config=self.app.screen_config(),
            tools_snapshot=self._mcp_screen_snapshot(),
        ))

    def _cmd_diff(self) -> None:
        from tui.screens.diff import DiffScreen
        cfg = self.app.screen_config()
        launch_root = cfg.get("launch_root")
        if not launch_root:
            self._mount_trace_row(
                prefix="· ",
                content="diff: launch_root не задан",
                kind_class="line-meta",
            )
            return
        threads_dir, system_id, thread_id = self._diff_thread_identity()
        self.app.push_screen(
            DiffScreen(
                launch_root=str(launch_root),
                config=cfg,
                on_undo_feedback=self._on_diff_undo_feedback,
                on_ui_ack=self._on_diff_ui_ack,
                threads_dir=threads_dir,
                system_id=system_id,
                thread_id=thread_id,
            )
        )


    def _on_diff_ui_ack(self, text: str) -> None:
        """TUI-only строка в трейсе после принять/откатить (не в thread модели)."""
        if not text:
            return
        self._mount_trace_row(
            prefix="", content=text, kind_class="line-meta", focusable=False,
        )

    def _on_diff_undo_feedback(self, restored_keys, deleted_keys=()) -> None:
        """T15: после undo — technical user message агенту (визуал — через on_ui_ack)."""
        from diff_tui.feedback import build_undo_feedback_message

        msg = build_undo_feedback_message(
            restored_keys=restored_keys,
            deleted_keys=deleted_keys,
        )
        if msg is None:
            return
        self._append_technical_to_thread(msg)

    def _should_arm_diff_cta(self) -> bool:
        """Стоит ли напоминать про диффы по итогам хода.

        Ноль событий от агента = ход ничего не сделал (отмена по Esc до первого
        ответа — тот же счётчик, на котором держится _cancel_with_undo). Диффы
        измениться не могли, а откат уже убрал реплику из трейса: концовка
        «небывшего» хода читается как мусор (смок 2026-08-02)."""
        return self._events_since_last_user_send > 0

    def _flush_diff_pending_cta(self) -> None:
        """Смонтировать post-turn CTA, если флаг выставлен и runner idle.

        Правило B проверяем здесь, а не в момент взвода: _events_since_last_user_send
        для текущего тика обновляется в _render_records/_render_event, которые
        внутри _poll_active выполняются ПОСЛЕ падающего фронта is_running. На
        момент взвода счётчик мог быть ещё нулевым, хотя хвост событий этого
        же тика вот-вот отрендерится (напр. ошибка провайдера прилетает
        быстрее одного polling-интервала) — решение по счётчику раньше
        времени отсекло бы законный CTA.
        reason="wipe_restore" правилу B не подчиняется: это не про итог хода,
        а про возврат строки, физически стёртой перерисовкой трейса."""
        reason = self._diff_pending_cta_pending
        if not reason:
            return
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and runner.is_running():
            return
        self._diff_pending_cta_pending = None
        if reason == "turn_end" and not self._should_arm_diff_cta():
            return
        self._maybe_mount_diff_pending_cta()

    def _maybe_mount_diff_pending_cta(self) -> None:
        """После turn: если есть незаапрувленные диффы — пути + ``F3/diff``."""
        from diff_tui.feedback import format_pending_cta

        svc = self._diff_service_for_preview()
        if svc is None:
            return
        try:
            keys = [e.key for e in svc.list_entries(pending_only=True)]
        except Exception:
            return
        text = format_pending_cta(keys)
        if not text:
            return
        # Отменённый ход — тоже ход: спадающий фронт is_running зовёт нас снова.
        # Если состав диффов не менялся и CTA уже стоит последним — повтор ничего
        # не сообщает, а копит одинаковые блоки (смок 2026-08-02). Дальше первой
        # строки не смотрим: вывод между ходами делает напоминание снова уместным.
        if self._last_trace_row_content() == text:
            return
        self._mount_trace_row(
            prefix="", content=text, kind_class="line-meta", focusable=False,
        )

    def _append_technical_to_thread(self, msg: dict) -> None:
        tid = self._active_task_id or getattr(
            getattr(self.app, "tui_state", None), "active_task_id", None
        )
        if not tid:
            return
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and runner.is_running():
            runner.add_message(msg)
            return
        threads_dir = getattr(self.app, "_threads_dir", None)
        system = getattr(self.app, "_system", "cli")
        if not threads_dir:
            return
        from thread_store import (
            append_new_thread_messages,
            load_thread_messages,
            load_thread_meta,
            save_thread_meta,
        )

        _msgs, keys, max_seq = load_thread_messages(threads_dir, system, tid)
        written = append_new_thread_messages(
            threads_dir,
            system,
            tid,
            [msg],
            keys,
            start_seq=max_seq,
        )
        if written and self._store is not None:
            meta = load_thread_meta(threads_dir, system, tid) or {}
            try:
                meta["messages_count"] = int(meta.get("messages_count", 0)) + written
            except (TypeError, ValueError):
                meta["messages_count"] = written
            save_thread_meta(threads_dir, system, tid, meta)

    def _cmd_new(self) -> None:
        self._resumed_prompt = None                      # новая сессия — без resume-промпта
        self._last_terminal_title = format_terminal_title()
        set_terminal_title(self._last_terminal_title)
        runner = getattr(self.app, "active_runner", None)
        is_running = runner is not None and runner.is_running()
        if not is_running:
            # В прежнем run executor мог оставить in-memory usage-метрики
            # (футер берёт их через runner.usage_snapshot()).
            # Для новой сессии нужен clean executor — иначе футер показывает
            # токены/стоимость прошлой сессии.
            self._stop_and_replace_runner()
            self._switch_to_empty_state()
            return
        from tui.screens.confirm import ConfirmScreen

        def _on_result(confirmed: bool) -> None:
            if confirmed:
                self._stop_and_replace_runner()
                self._switch_to_empty_state()

        self.app.push_screen(
            ConfirmScreen(
                title="Новая задача",
                message="Активная задача в процессе.\nОстановить и начать новую?",
                hint="[Y] да, прервать   [N] нет, продолжить",
            ),
            _on_result,
        )

    def _request_exit(self) -> None:
        """Модалка подтверждения выхода (Esc/Ctrl+C вхолостую)."""
        from tui.screens.confirm import ConfirmScreen

        def _on_exit(confirmed: bool) -> None:
            if confirmed:
                self.app.exit()

        self.app.push_screen(
            ConfirmScreen(
                title="Выход",
                message="Завершить сессию?",
                hint="[Y] да, выйти   [N] нет, остаться",
            ),
            _on_exit,
        )

    def _cmd_stop(self) -> None:
        # Bang-shell: сначала cancel `!`; повторный Ctrl+C / /stop — stop агента.
        if self._bang is not None and self._bang.is_running:
            self._bang.cancel()
            return

        runner = self.app.active_runner
        # I2: /stop всегда разблокирует confirm (даже без смонтированного баннера).
        if runner is not None and hasattr(runner, "cancel_confirm"):
            runner.cancel_confirm()
        if self._confirm_active:
            self._hide_confirm_banner()
            if runner is not None and runner.is_running():
                # live agent-run confirm → also stop the run (Ctrl+C = abort)
                if self._thinking is not None:
                    self._thinking.set_state("cancelled")
                self._stop_and_replace_runner()
            else:
                # /init (or no live runner): parked worker now unblocked → cancel/deny
                self._mount_trace_row(prefix="· ", content="confirm отменён",
                                      kind_class="line-meta")
            return

        if runner is None or not runner.is_running():
            self._mount_trace_row(
                prefix="· ", content="нет активного запуска",
                kind_class="line-meta",
            )
            return

        # Cancel-with-undo (агент не успел ничего выдать + input пустой) — отдельно.
        input_text = ""
        try:
            input_text = self.query_one("#cmdinput").text
        except Exception:
            pass
        if self._events_since_last_user_send == 0 and not input_text:
            self._cancel_with_undo()
            return

        # Normal interrupt: индикатор → терминальное «■ Отменено» (статус
        # [stopped] уже справа на той же строке), без отдельной [stopped]-строки.
        if self._thinking is not None:
            self._thinking.set_state("cancelled")
        self._stop_and_replace_runner()

    def _stop_and_replace_runner(self) -> None:
        """Кооперативный stop + mark + обнуление active_runner.

        Следующий self.app.get_runner() создаст свежий — clean restart для
        нового user-запроса. Старый runner может ещё досидеть HTTP в фоне;
        благодаря discard_writes_after_stop его output в thread_store не уйдёт.
        """
        runner = self.app.active_runner
        if runner is not None:
            # Стоп ЖИВОГО прогона → терминальный статус «stopped». Флаг app-уровня
            # (не runner.stop_requested): ниже active_runner обнуляется, и
            # спадающий фронт в _poll_active уже не увидит этот runner.
            if runner.is_running():
                self.app.last_stop_requested = True
            runner.stop()
            runner._marked_stopped = True
        self.app.active_runner = None

    def _stop_for_ask_human(self) -> None:
        """Esc в live ask_human → clean stop треда.

        Шаги:
        1. Разблокировать agent-thread, заблокированный в input() внутри
           ask_human-тула: положить пустую строку в stdin-queue через
           runner.cancel_ask_human() (контракт по сути «unblock stdin»,
           а не «cancel» в семантике юзера).
        2. Снять banner + сбросить tui_state.ask_human_state.
        3. Смонтировать [stopped]-метку в трейсе.
        4. Погасить ThinkingIndicator.
        5. runner.stop() + active_runner = None (clean-restart pattern).

        После этого state.stop_requested=True; agent-loop выйдет на следующей
        проверке. discard_writes_after_stop+stop_requested блокирует persist
        синтетического function_call_output от тула → в .jsonl ничего не
        запишется (нет повторного re-mount banner-а через mapper).

        Следующий submit юзера: get_runner() создаст свежий runner,
        runner.start(query=text, thread_id=<existing>) продолжит тот же тред."""
        runner = self.app.active_runner
        if runner is None or not runner.is_running():
            return
        runner.cancel_ask_human()
        self._unmount_ask_human_banner()
        if self._thinking is not None:
            self._thinking.set_state("cancelled")
        self._stop_and_replace_runner()

    def _cancel_with_undo(self) -> None:
        """Откатить последний user-send: убрать user-widget, восстановить input.

        Условие срабатывания (проверено в _cmd_stop):
        - 0 trace-событий от агента с момента последнего user-send.
        - input пуст.

        Реплика к этому моменту уже могла быть записана в тред (persist user-
        сообщения происходит до ответа бэкенда). Поэтому, помимо визуального
        отката, ставим заявку на удаление этой записи по id — она исполнится в
        poll, когда тред станет idle (см. _run_orphan_cleanups): без гонок с
        дозаписью worker'а.
        """
        from tui.widgets.user_reply import UserReplyContainer

        # Заявка на удаление осиротевшей реплики (по id, когда тред idle).
        old_runner = getattr(self.app, "active_runner", None)
        tid = self.app.tui_state.active_task_id
        msg_id = self._last_user_message_id
        if tid is not None and msg_id and self._store is not None:
            self._pending_orphan_cleanups.append((old_runner, tid, msg_id))

        # Удалить последний UserReplyContainer
        user_widgets = list(self.query(UserReplyContainer))
        if user_widgets:
            user_widgets[-1].remove()

        # Полный откат: убрать pending-запись из meta (иначе на re-entry
        # _mount_failed_from_meta покажет её как фантомную [!] failed-реплику).
        cid = self._last_pending_client_id
        if cid is not None:
            self._remove_pending_record(cid)
            self._pending_user_widgets.pop(cid, None)
            self._last_pending_client_id = None

        # Восстановить текст в input + поставить курсор в конец, чтобы
        # юзер мог сразу продолжить печатать (а не правил с начала строки).
        try:
            input_field = self.query_one("#cmdinput")
            input_field.text = self._last_user_send_text
            input_field.cursor_location = input_field.document.end
        except Exception:
            pass

        # Убить индикатор + заменить runner — как в normal stop
        self._destroy_thinking_indicator()
        self._stop_and_replace_runner()

        # Немедленно удалить осиротевшую реплику из треда: worker уже остановлен
        # флагом и после stop в data_*.jsonl не пишет (запись реплики, если была,
        # уже на диске; вывод агента — под discard-guard). Так после re-entry её
        # не будет, даже если HTTP-вызов worker'а ещё висит на флапающем бэкенде.
        # Заявка _pending_orphan_cleanups (см. выше) — backstop на микро-гонку
        # «persist ровно в момент удаления».
        if tid is not None and msg_id:
            self._discard_orphan_and_resync(tid, msg_id)
        # НЕ монтируем [stopped] — это и есть "undo".

    def _discard_orphan_and_resync(self, tid: str, msg_id: str) -> bool:
        """Удалить запись реплики по id и пересинхронизировать курсор трейса.

        Без ресинка poll увидит ужавшийся data_*.jsonl как «file truncated» →
        stream_reset → полная перерисовка трейса (моргание). Здесь курсор
        сдвигается на новый конец файла, поэтому тик ничего не перерисовывает —
        виджет уже снят точечно."""
        if self._store is None:
            return False
        try:
            removed = self._store.discard_message_by_id(tid, msg_id)
        except Exception:
            return False
        if removed and tid == self._active_task_id and self._cursor is not None:
            try:
                _records, cur = self._store.read_all_events(tid)
                self._cursor = cur
            except Exception:
                pass
        return removed

    def _run_orphan_cleanups(self) -> None:
        """Удалить осиротевшие реплики из треда, когда тред idle (нет активного
        run'а, пишущего этот файл) и worker отменённого run'а уже остановлен.
        Вызывается из poll как backstop. id-based → точечно и без гонок."""
        if not self._pending_orphan_cleanups:
            return
        active = getattr(self.app, "active_runner", None)
        active_tid = self.app.tui_state.active_task_id
        active_running = active is not None and active.is_running()
        still_pending: list[tuple] = []
        for old_runner, tid, msg_id in self._pending_orphan_cleanups:
            old_busy = old_runner is not None and old_runner.is_running()
            thread_busy = active_running and active_tid == tid
            if old_busy or thread_busy:
                still_pending.append((old_runner, tid, msg_id))
                continue
            self._discard_orphan_and_resync(tid, msg_id)
        self._pending_orphan_cleanups = still_pending

    def _reset_history_navigation(self) -> None:
        """Выход из режима history-навигации. Зовётся при правке input или send."""
        self._history_index = None
        self._history_draft = None
        self._history_last_programmatic_text = None

    def _history_prev(self) -> None:
        """Шаг назад по истории. На первый вызов — сохранить draft."""
        if self._history is None or self._history.size == 0:
            return
        if self._history_index is None:
            try:
                input_field = self.query_one("#cmdinput")
                self._history_draft = input_field.text
            except Exception:
                self._history_draft = ""
            self._history_index = self._history.size
        result = self._history.prev(self._history_index)
        if result is None:
            return
        new_index, text = result
        self._history_index = new_index
        self._history_last_programmatic_text = text
        try:
            input_field = self.query_one("#cmdinput")
            input_field.text = text
            input_field.cursor_location = input_field.document.end
        except Exception:
            pass

    def _history_next(self) -> None:
        """Шаг вперёд по истории. За newest — вернуть draft."""
        if self._history is None or self._history_index is None:
            return
        result = self._history.next(self._history_index)
        if result is None:
            draft_text = self._history_draft or ""
            # Возврат к draft: технически это «выход» из навигации,
            # но Changed для draft-текста придёт ПОСЛЕ обнуления state
            # → ничего не нужно ловить, элиф ниже его игнорирует.
            try:
                input_field = self.query_one("#cmdinput")
                input_field.text = draft_text
                input_field.cursor_location = input_field.document.end
            except Exception:
                pass
            self._history_index = None
            self._history_draft = None
            self._history_last_programmatic_text = None
            return
        new_index, text = result
        self._history_index = new_index
        self._history_last_programmatic_text = text
        try:
            input_field = self.query_one("#cmdinput")
            input_field.text = text
            input_field.cursor_location = input_field.document.end
        except Exception:
            pass

    def _cmd_use_skill(self, skill_id: str, *, prompt: str = "") -> None:
        if not skill_id:
            self._mount_trace_row(
                prefix="[!] ", content="/skill: укажите id навыка",
                kind_class="line-error",
            )
            return
        if prompt:
            self._deliver_skill_with_prompt(skill_id, prompt)
            return
        runner = self.app.active_runner
        if runner is not None and runner.is_running():
            # Идёт run → подкидываем как сообщение и фиксируем в meta.
            from tools.skills.registry import get_skills_registry
            from tools.skills.skill_delivery import build_skill_delivery, skill_is_available
            reg = get_skills_registry(getattr(self.app, "_config", None) or {})
            loaded = skill_is_available(skill_id, reg)
            msg = {"role": "user", "content": build_skill_delivery(skill_id, reg),
                   "display_content": ""}
            if loaded:
                msg["skill_loaded"] = skill_id
            runner.add_message(msg)
            self._record_skill_meta(skill_id)
            self._mount_skill_marker(skill_id, loaded)
            return
        # Run не идёт (нет задачи / завершена / открыта через /sessions) — отложенно
        # к следующему сообщению.
        self.app.tui_state.pending_skill_id = skill_id
        self._record_skill_meta(skill_id)
        self._mount_trace_row(
            prefix="[skill] ",
            content=f"`{skill_id}` применится к следующему сообщению",
            kind_class="line-meta",
        )

    def _deliver_skill_with_prompt(self, skill_id: str, prompt: str) -> None:
        """Inline /skill <id> <prompt>: префикс «Используй skill …» встраивается в
        реплику тем же _consume_pending_prefix, что и pending-путь — единая форма
        доставки. Technical-обёртка не используется: системный промпт трактует
        technical-блоки как контекст, не команды, и слабые модели вправе их
        игнорировать. Дисплей: маркер [skill] + реплика с чистым промптом."""
        self.app.tui_state.pending_skill_id = skill_id
        full_query, _ = self._consume_pending_prefix(prompt)
        _, skill_loaded = self._skill_display_args(prompt)
        msg_id = "m_" + uuid.uuid4().hex
        self._last_user_message_id = msg_id

        runner = self.app.active_runner
        if runner is not None and runner.is_running():
            from tools.subagents.active_scope import is_child_input_active

            msg = {"role": "user", "content": full_query, "id": msg_id,
                   "display_content": prompt}
            if skill_loaded:
                msg["skill_loaded"] = skill_loaded
            runner.add_message(msg)
            if not is_child_input_active():
                client_id = self._track_pending(
                    display_text=prompt, wire_content=full_query
                )
                self._mount_user_reply(prompt, state="pending", client_id=client_id)
            self._reset_thinking_timer()
            self._on_user_text_submitted(prompt)
            self._record_skill_meta(skill_id)
            return

        if self.app.tui_state.active_task_id is None:
            # AUTO-CREATE: зеркалим порядок auto-create в on_command_text_area_submitted.
            tid = self._begin_auto_created_task()
            runner = self.app.get_runner()
            self._prepare_for_new_run()
            self._reset_thinking_timer()
            self._on_user_text_submitted(prompt)
            runner.start(query=full_query, thread_id=tid, system=self.app._system,
                         message_id=msg_id,
                         display_query=prompt, skill_loaded=skill_loaded)
            # attach_active_task — после start, как в реальном auto-create;
            # сбрасывает _pending_user_widgets через _reset_render_state.
            self.attach_active_task(tid)
            # _track_pending и _mount_user_reply — ПОСЛЕ attach_active_task,
            # иначе _reset_render_state вычистит только что добавленный widget.
            self._record_skill_meta(skill_id)
            client_id = self._track_pending(display_text=prompt, wire_content=full_query)
            self._mount_user_reply(prompt, state="pending", client_id=client_id)
            return

        # Задача есть, но run не идёт — продолжаем тот же thread.
        # Зеркалим порядок continue-thread в on_command_text_area_submitted.
        runner = self.app.get_runner()
        client_id = self._track_pending(display_text=prompt, wire_content=full_query)
        self._mount_user_reply(prompt, state="pending", client_id=client_id)
        self._prepare_for_new_run()
        self._reset_thinking_timer()
        self._on_user_text_submitted(prompt)
        runner.start(query=full_query, thread_id=self.app.tui_state.active_task_id,
                     system=self.app._system, message_id=msg_id,
                     display_query=prompt, skill_loaded=skill_loaded)
        self._record_skill_meta(skill_id)

    def _record_skill_meta(self, skill_id: str) -> None:
        """Зафиксировать skill_id в meta.tui.skills активной задачи."""
        tid = self.app.tui_state.active_task_id
        if tid is not None and self._store is not None:
            current = (self._store.get_task_meta(tid).get("tui") or {}).get("skills", [])
            if skill_id not in current:
                self._store.update_tui_meta(tid, {"skills": current + [skill_id]})

    def _switch_to_empty_state(self) -> None:
        """Очистить trace и перейти в empty-state. pending_skill_id/cwd сохраняются."""
        # Сбросить терминальные error/stop-флаги app-уровня (last_runner_error и пр.):
        # иначе poll видит протухшую ошибку и репейнтит «✗ Ошибка» поверх пустой сессии.
        self._session_lock_release()
        self._prepare_for_new_run()
        self._active_task_id = None
        self.app.tui_state.active_task_id = None
        self._session_lock_hb_mono = 0.0
        self._store = None
        self._cursor = None
        self._reset_render_state()
        self._clear_trace()
        self._refresh_header()
        self._refresh_footer()
        self._sync_indicator_reserve()

    def _sync_indicator_reserve(self) -> None:
        """Высота #indicator-bar: 3 при активной задаче (отступ-строка снизу под
        индикатором), 2 на пустом старте (без задачи) — чтобы чистый экран не нёс
        лишнюю строку. В пределах состояния высота фиксирована → нет «прыжка»."""
        try:
            bar = self.query_one("#indicator-bar", Vertical)
        except Exception:
            return
        bar.styles.height = 3 if self._active_task_id is not None else 2

    def _handle_bang_shell(self, text: str) -> None:
        """`!command` → локальный shell в cwd TUI; результат только в трейсе."""
        from tui.services.bang_shell import (
            BangShellController,
            BangShellSettings,
            parse_body,
            parse_clear_meta,
            preflight,
        )

        body = parse_body(text)
        if body is None:
            self._mount_bang_row(
                prefix="[!] ",
                content="пустая команда",
                kind_class="line-bang-fail",
            )
            self._close_bang_block()
            return

        clear_meta, clear_err = parse_clear_meta(body)
        if clear_err is not None:
            self._mount_bang_row(
                prefix="[!] ",
                content=clear_err,
                kind_class="line-bang-fail",
            )
            self._close_bang_block()
            return
        if clear_meta is not None:
            self._clear_bang_blocks(clear_meta.count)
            return

        cfg = getattr(self.app, "_config", None) or {}
        settings = BangShellSettings.from_mapping(cfg.get("tui_bang_shell"))
        self._bang_timeout_sec = settings.timeout_sec

        self._mount_bang_row(
            prefix="[!] ",
            content=f"$ {body}",
            kind_class="line-bang",
        )

        if not settings.enabled:
            self._mount_bang_row(
                prefix="   ",
                content="bang-shell выключен",
                kind_class="line-bang-fail",
            )
            self._close_bang_block()
            return

        if self._bang is None:
            self._bang = BangShellController()

        if self._bang.is_running:
            self._mount_bang_row(
                prefix="   ",
                content="дождитесь завершения предыдущей !",
                kind_class="line-bang-fail",
            )
            self._close_bang_block()
            return

        reason = preflight(body)
        if reason is not None:
            self._mount_bang_row(
                prefix="   ",
                content=reason,
                kind_class="line-bang-fail",
            )
            self._close_bang_block()
            return

        bang = self._bang
        cwd = self._current_cwd()

        def _work() -> None:
            result = bang.run(body, cwd=cwd, settings=settings)
            try:
                self.app.call_from_thread(self._on_bang_done, result)
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()

    def _mount_bang_row(
        self,
        *,
        prefix: str,
        content: str,
        kind_class: str,
    ):
        """Смонтировать bang-строку и учесть её в текущем блоке для `! clear`."""
        row = self._mount_trace_row(
            prefix=prefix,
            content=content,
            kind_class=kind_class,
            focusable=False,
        )
        if self._bang_open_block is None:
            self._bang_open_block = []
            self._bang_blocks.append(self._bang_open_block)
        self._bang_open_block.append(row)
        return row

    def _close_bang_block(self) -> None:
        self._bang_open_block = None

    def _clear_bang_blocks(self, count: Optional[int]) -> None:
        """Удалить bang-блоки из трейса. count=None — все; иначе последние N."""
        if self._bang is not None and self._bang.is_running:
            self._mount_bang_row(
                prefix="[!] ",
                content="дождитесь завершения предыдущей !",
                kind_class="line-bang-fail",
            )
            self._close_bang_block()
            return
        if not self._bang_blocks:
            return
        if count is None or count >= len(self._bang_blocks):
            to_remove = self._bang_blocks[:]
            self._bang_blocks.clear()
        else:
            to_remove = self._bang_blocks[-count:]
            del self._bang_blocks[-count:]
        self._bang_open_block = None
        for block in to_remove:
            for row in block:
                try:
                    row.remove()
                except Exception:
                    pass

    def _on_bang_done(self, result) -> None:
        """Смонтировать результат bang-shell в трейс (UI-поток)."""
        if result.rejected:
            self._mount_bang_row(
                prefix="   ",
                content=result.rejected,
                kind_class="line-bang-fail",
            )
            self._close_bang_block()
            return
        if result.cancelled:
            self._mount_bang_row(
                prefix="   ",
                content="[!] cancelled",
                kind_class="line-bang-fail",
            )
            self._close_bang_block()
            return
        if result.timed_out:
            n = self._bang_timeout_sec
            label = f"[!] timeout after {int(n)}s" if n else "[!] timeout"
            self._mount_bang_row(
                prefix="   ",
                content=label,
                kind_class="line-bang-fail",
            )
            self._close_bang_block()
            return

        out = (result.output or "").rstrip("\n")
        if not out and result.exit_code == 0:
            body = "(нет вывода)"
        else:
            body = out
        code = result.exit_code
        if code is not None:
            content = f"{body}\n[exit {code}]" if body else f"[exit {code}]"
        else:
            content = body or "(нет вывода)"
        kind = "line-bang-out" if result.ok else "line-bang-fail"
        self._mount_bang_row(
            prefix="   ",
            content=content,
            kind_class=kind,
        )
        self._close_bang_block()

    def _handle_command(self, raw: str) -> None:
        """Диспетчеризовать команду и, если она что-то дописала в трейс,
        форсировать скролл вниз. Команда — прямое действие пользователя, поэтому
        её результат показываем безусловно, даже если юзер отскроллен вверх
        (в отличие от агентских событий). Команды, открывающие экран, трейс не
        растят → счётчик не меняется → позиция чтения истории не сбивается."""
        trace = self.query_one("#trace", VerticalScroll)
        before = len(trace.children)
        self._dispatch_command(raw)
        if len(trace.children) > before:
            self._stick_bottom = True   # команда юзера = re-engage
            self._scroll_trace_end()

    def _dispatch_command(self, raw: str) -> None:
        # Имя команды + остаток строки как единственный аргумент, дословно.
        # Без shlex: путь (D:\dev\...) берётся как есть, без escape/кавычек.
        raw = raw.strip()
        if not raw:
            return
        parts = raw.split(None, 1)
        name = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        cmd = self._registry.lookup(name)
        if cmd is None:
            self._render_command_error(name, closest_command(name, self._registry))
            return
        # Нормализуем alias → canonical, чтобы диспетчер видел только канон.
        name = cmd.name
        if name == "quit":
            self.app.exit()
            return
        if name == "sessions":
            self.app.action_sessions()
            return
        if name == "new":
            self._cmd_new()
            return
        if name == "skill":
            if self._skills_cache is None:
                self._skills_cache = self._load_skills_safe()
            first, _, rest = arg.partition(" ")
            rest = rest.strip()
            if rest and any(str(s.get("id")) == first for s in self._skills_cache):
                # /skill <валидный_id> <промпт> → немедленная пара
                self._cmd_use_skill(first, prompt=rest)
            else:
                # первый токен не валидный id → весь arg = id (может содержать
                # пробелы/слэши), промпта нет → штатный pending/running путь
                self._cmd_use_skill(arg.strip(), prompt="")
            return
        if name == "help":
            from tui.screens.help import HelpScreen
            self.app.push_screen(HelpScreen())
            return
        if name == "skills":
            from tui.screens.skills import SkillsScreen
            self.app.push_screen(SkillsScreen(
                skills=self._load_skills_safe(),
                config=self.app.screen_config(),
            ))
            return
        if name == "config":
            self._cmd_config()
            return
        if name == "settings":
            self._cmd_settings()
            return
        if name == "mcp":
            self._cmd_mcp()
            return
        if name == "buffer":
            self.app.action_buffer()
            return
        if name == "diff":
            self.app.action_diff()
            return
        if name == "view":
            self._cmd_view(arg)
            return
        if name == "notices":
            self.app.action_notices()
            return
        if name == "stop":
            self._cmd_stop()
            return
        if name == "export":
            self._cmd_export(arg)
            return
        if name == "status":
            self._cmd_status(arg)
            return
        if name == "init":
            self._cmd_init()
            return
        if name == "compact":
            self._cmd_compact()
            return
        if name == "voice":
            self._voice_toggle()
            return
        if name == "profiles":
            self._cmd_profiles(arg)
            return
        # Тоггл-команды опознаются по флагу реестра (develop обобщил перечисление
        # имён): /voice и /profiles выше — не тогглы, а действия, поэтому идут своими
        # ветками.
        if cmd.is_toggle:
            self._toggle_info_command(name)
            return

    def _cmd_profiles(self, arg: str) -> None:
        """`/profiles [профиль]` (спека §12.1, ревизия 2026-07-28): без аргумента —
        раскрыть пикер; «—» — сессионное снятие профиля (§9.1: работать чистым
        конфигом); имя — сессионный выбор; неизвестное имя — честный отказ."""
        value = arg.strip()
        if not value:
            self._open_profiles_picker()
            return
        if value == PROFILE_NONE_VALUE:
            target = ""
        else:
            from tui.toggles import effective_toggle_names
            available = effective_toggle_names(self.app, "profiles")
            if value not in available:
                tail = f" · доступно: {', '.join(available)}" if available else ""
                self._show_footer_hint(f'✗ профиль "{value}" не найден{tail}')
                return
            target = value
        # Единственная точка вызова на оба исхода: `apply_session_override`
        # осознанно ре-райзит OSError после отката (каталог тредов недоступен,
        # диск полон), а пере-мерж — ValueError, если файл профиля исчез между
        # листингом и выбором. Здесь мы внутри обработчика клавиш: непойманное
        # исключение уронило бы приложение. Тот же «✗ не сохранено», что и в
        # палитре (см. `_on_key` ветку apply_toggle).
        try:
            self.app.switch_session_profile(target)
        except (ValueError, OSError) as exc:
            self._show_footer_hint(f"✗ не сохранено: {exc}")

    def _toggle_info_command(self, name: str) -> None:
        """`/gate` или `/answers`, набранные и подтверждённые текстом (не через
        стрелки палитры — у toggle-команд нет args-схемы, поэтому пробел после
        имени закрывает палитру и Enter доходит сюда как обычная команда):
        ничего не меняет (иначе набор команды был бы неочевидным side-effect'ом) —
        показывает текущее значение и как переключать (←/→ и Enter в открытой
        палитре)."""
        current = toggle_state(self.app, name)
        self._show_footer_hint(
            f"/{name} — текущее значение: {current} · для переключения: ←/→ и Enter в меню команд",
            seconds=5.0,
        )

    def _open_event_by_no(self, no: int) -> None:
        """Открыть полный payload события №no в Viewer.
        Триггер — двойной клик по строке tool-вызова / агентской реплики."""
        for ev in self._task_events:
            if ev.no == no:
                from tui.screens.viewer import ViewerScreen
                # ViewerScreen сам делает render_payload — pretty-print JSON
                # с solarized-dark подсветкой для dict/list или plain Static
                # для остального. Передаём raw payload без предварительного dumps.
                # summary: в segments-пути full_payload пуст ({}); показываем
                # синтез сводки (summary_content заполнен в обоих путях).
                content = (
                    ev.summary_content if ev.kind == "summary" else ev.full_payload
                )
                self.app.push_screen(ViewerScreen(
                    title=f"event {no} · {ev.tool_name or ev.kind}",
                    content=content,
                ))
                return
        self._mount_trace_row(
            prefix="[!] ", content=f"нет события {no}",
            kind_class="line-error",
        )

    def on_open_event_request(self, message) -> None:
        """Двойной клик по строке tool-вызова/реплики → открыть payload."""
        self._open_event_by_no(message.event_no)

    def on_summary_toggle_request(self, message) -> None:
        """Клик/Enter по маркеру или телу → раскрыть/свернуть summary-body."""
        body = self._summary_body_widgets.get(message.event_no)
        if body is None:
            return
        marker = self._summary_marker_widgets.get(message.event_no)
        if body.has_class("hidden"):
            body.remove_class("hidden")
            if marker is not None:
                marker.set_prefix("▾ ")
            # Раскрыли — подвести тело в зону видимости после reflow.
            self.call_after_refresh(body.scroll_visible, animate=False)
        else:
            body.add_class("hidden")
            if marker is not None:
                marker.set_prefix("▸ ")

    def on_copy_block_request(self, event: CopyBlockRequest) -> None:
        """Кнопка ❐ или двойной клик по блоку: копия наружу + в буфер сессии.

        Буфер сессии — как у Ctrl+C: это явный жест копирования, а не побочный
        эффект выделения. Двойной клик перед этим сам рождает TextSelected
        (выделение тела) и ставит в очередь отложенную автокопию видимого —
        её нужно отменить, иначе она затрёт payload позже (гонка copy-on-select)."""
        payload = event.payload
        if not payload:
            return
        self.app.cancel_pending_select_copy()
        copy_out(self.app, payload, toast=True, blocking=False)
        self.app._push_session_copy_buffer(payload)

    def on_open_tool_call_detail_request(self, message) -> None:
        """Ctrl+click / Enter по row → детальный экран call + output.

        Parent-тулы — из ``_task_events`` (data_*). Nested child — из
        sidecar ``data_subagents_*``.
        """
        call_ev = None
        result_ev = None
        for ev in self._task_events:
            if ev.call_id != message.call_id:
                continue
            if ev.kind == "call":
                call_ev = ev
            elif ev.kind in ("result", "error"):
                result_ev = ev
        if call_ev is None:
            call_ev, result_ev = self._resolve_nested_detail_events(message.call_id)
        if call_ev is None:
            if message.call_id and message.call_id in self._nested_tool_rows:
                self._show_footer_hint(
                    "нет args/output для этого nested-тула", seconds=3.0
                )
            return
        self.app.push_screen(ToolCallDetailScreen(
            call_event=call_ev, result_event=result_ev,
        ))

    def _resolve_nested_detail_events(self, call_id: str):
        """TraceEvent pair для nested tool; нет данных → (None, None)."""
        if not call_id:
            return None, None
        records = None
        if self._store is not None and self._active_task_id:
            records = self._store.read_subagent_trace(self._active_task_id)
        detail = resolve_nested_tool_detail(call_id, sidecar_records=records)
        if not detail or not isinstance(detail.get("call"), dict):
            return None, None
        return trace_events_from_nested_detail(call_id, detail)

    def _load_skills_safe(self) -> list[dict]:
        """Загрузить список скиллов из core-реестра; на ошибке вернуть []."""
        try:
            from tools.skills.registry import get_skills_registry
            reg = get_skills_registry(getattr(self.app, "_config", None) or {})
            # include_all=True — показываем и внутренние скиллы паков, не только bootstrap.
            return reg.list_skills(include_all=True)
        except Exception as exc:
            self._mount_trace_row(
                prefix="[!] ", content=f"/skills: не удалось загрузить ({exc})",
                kind_class="line-error",
            )
            return []

    def _cmd_export(self, arg: str) -> None:
        fmt = (arg or "").strip() or "txt"
        if fmt not in ("txt", "json", "log-txt", "log-json"):
            self._mount_trace_row(
                prefix="[!] ", content=f"/export: формат {fmt!r} не поддерживается",
                kind_class="line-error",
            )
            return
        tid = self._active_task_id
        if not tid:
            self._mount_trace_row(
                prefix="[!] ", content="/export: нет активной задачи",
                kind_class="line-error",
            )
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        if fmt == "log-txt":
            self._export_log_txt(tid, stamp)
            return
        if self._store is None:
            self._mount_trace_row(
                prefix="[!] ", content="/export: нет активной задачи",
                kind_class="line-error",
            )
            return
        if fmt == "log-json":
            raw = self._store.read_raw_thread_jsonl(tid)
            path = Path.cwd() / f"export-{tid}-{stamp}-log.jsonl"
            path.write_text(raw, encoding="utf-8")
            self._mount_trace_row(
                prefix="[exported] ", content=str(path), kind_class="line-meta",
            )
            return
        segments = self._store.read_segments(tid)
        meta = self._store.get_task_meta(tid)
        text = export_thread(segments, fmt=fmt, meta=meta,
                             thread_id=tid, system=self.app._system)
        path = Path.cwd() / f"export-{tid}-{stamp}.{fmt}"
        path.write_text(text, encoding="utf-8")
        self._mount_trace_row(
            prefix="[exported] ", content=str(path), kind_class="line-meta",
        )

    def _export_log_txt(self, tid: str, stamp: str) -> None:
        log_path = getattr(self.app, "_session_log_path", None)
        if not log_path:
            self._mount_trace_row(
                prefix="[!] ",
                content="/export: лог сессии ещё не создан — сначала сделайте запрос",
                kind_class="line-error",
            )
            return
        if str(log_path).startswith("Null"):
            self._mount_trace_row(
                prefix="[!] ", content="/export: логи отключены (save_logs: false)",
                kind_class="line-error",
            )
            return
        src = Path(log_path)
        if not src.exists():
            self._mount_trace_row(
                prefix="[!] ", content=f"/export: файл лога не найден: {src}",
                kind_class="line-error",
            )
            return
        dst = Path.cwd() / f"export-{tid}-{stamp}-{src.name}"
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        self._mount_trace_row(
            prefix="[exported] ", content=str(dst), kind_class="line-meta",
        )

    def _cmd_status(self, arg: str) -> None:
        """Дамп meta активной задачи + runtime-состояние TUI в Viewer.

        Флаг `--raw` принимается в `arg`, но MVP-формат один (JSON с отступами);
        семантика raw vs formatted зарезервирована за будущим.
        """
        import json
        from tui.screens.viewer import ViewerScreen

        tid = self.app.tui_state.active_task_id
        payload: dict = {}

        if tid is not None and self._store is not None:
            payload["meta"] = self._store.get_task_meta(tid)
            title_id = tid[:8]
        else:
            payload["meta"] = None
            title_id = "нет задачи"

        runner = self.app.active_runner
        ah = self.app.tui_state.ask_human_state
        cfg = self.app.effective_config()
        router = self.app.display_router()
        # check_breaker=False: /status показывает модель честно даже при открытом
        # активном алиасе (спека §12.2); недоступность отражаем отдельным флагом.
        resolved = router.resolve("main", check_breaker=False)
        breaker_open = bool(resolved.alias) and router.breaker.is_open(resolved.alias)
        ref = self.app.active_profile_ref()
        payload["runtime"] = {
            "active_task_id": tid,
            "runner_is_running": bool(runner and runner.is_running()),
            "pending_skill_id": self.app.tui_state.pending_skill_id,
            "ask_human": (
                {"call_id": ah.call_id, "question": ah.question}
                if ah is not None else None
            ),
            "last_runner_error": getattr(self.app, "last_runner_error", None),
            "profile": {
                "name": cfg.get("profile") or None,
                "source": ("проектный" if ref is not None and ref.root_kind == "project"
                           else "глобальный" if ref is not None else None),
                "level": router.active_level(),
                "model": resolved.alias or resolved.model,
                "circuit_breaker_open": breaker_open,
            },
        }

        self.app.push_screen(ViewerScreen(
            title=f"status · {title_id}",
            content=json.dumps(payload, ensure_ascii=False, indent=2),
        ))

    def _cmd_compact(self) -> None:
        """/compact — принудительно сжать сессию (гарды: режим/простой/контент)."""
        cfg = getattr(self.app, "_config", None) or {}
        if str(cfg.get("backend_api_mode")) != "chat_completions":
            self._mount_trace_row(prefix="· ",
                                  content="/compact недоступен в режиме responses",
                                  kind_class="line-meta")
            return
        tid = self._active_task_id
        runner = getattr(self.app, "active_runner", None)
        if tid is None or self._store is None:
            self._mount_trace_row(prefix="· ", content="/compact: нет активной сессии",
                                  kind_class="line-meta")
            return
        if (runner is not None and runner.is_running()) \
                or self._task_status_badge() in ("[running]", "[asking]"):
            self._mount_trace_row(prefix="· ",
                                  content="/compact: дождитесь паузы или /stop",
                                  kind_class="line-meta")
            return
        # Антиспам: runtime.live_msg_count vs tui.last_compact_msg_count.
        # baseline пишет _poll_active на завершении сжатия. Нет baseline → можно.
        meta = self._store.get_task_meta(tid)
        tui_meta = meta.get("tui") or {}
        rt = meta.get("runtime") or {}
        last_compact = tui_meta.get("last_compact_msg_count")
        live_count = rt.get("live_msg_count")
        if (
            last_compact is not None
            and live_count is not None
            and int(live_count) <= int(last_compact)
        ):
            self._mount_trace_row(
                prefix="· ",
                content="/compact: после последнего сжатия ничего не добавилось — сжимать нечего",
                kind_class="line-meta")
            return
        if runner is None:
            runner = self.app.get_runner()
        # Сброс якоря таймера: «сжимаю контекст…» должен считать с 0, а не от
        # момента прошлого хода (как и на обычном send).
        self._reset_thinking_timer()
        runner.compact_now(tid, self.app._system)

    def _cmd_init(self) -> None:
        """/init: видимый prompt-driven прогон — агент сам пишет AGENTS.md.

        Зеркалит _deliver_skill_with_prompt: init-промпт идёт как ОБЫЧНОЕ user-сообщение
        (wire), в трейсе — маркер "/init" (display). busy-guard отбивает активный прогон,
        поэтому здесь только два случая: auto-create треда и continue-thread."""
        runner = self.app.get_runner()
        if runner.is_running():
            self._mount_trace_row(
                prefix="[!] ", content="/init: дождитесь завершения текущей задачи",
                kind_class="line-error",
            )
            return

        from instructions.init_agents_md import build_init_prompt
        wire = build_init_prompt(cwd=os.getcwd(), can_ask=True)
        display = "/init"
        msg_id = "m_" + uuid.uuid4().hex
        self._last_user_message_id = msg_id

        if self.app.tui_state.active_task_id is None:
            tid = self._begin_auto_created_task()
            runner = self.app.get_runner()
            self._prepare_for_new_run()
            self._reset_thinking_timer()
            self._on_user_text_submitted(display)
            runner.start(query=wire, thread_id=tid, system=self.app._system, message_id=msg_id)
            self.attach_active_task(tid)
            client_id = self._track_pending(display_text=display, wire_content=wire)
            self._mount_user_reply(display, state="pending", client_id=client_id)
            self._mount_trace_row(prefix="", content="/init: запускаю агента", kind_class="line-meta")
            return

        runner = self.app.get_runner()
        client_id = self._track_pending(display_text=display, wire_content=wire)
        self._mount_user_reply(display, state="pending", client_id=client_id)
        self._mount_trace_row(prefix="", content="/init: запускаю агента", kind_class="line-meta")
        self._prepare_for_new_run()
        self._reset_thinking_timer()
        self._on_user_text_submitted(display)
        runner.start(query=wire, thread_id=self.app.tui_state.active_task_id,
                     system=self.app._system, message_id=msg_id)

    def _render_command_error(self, name: str, suggestion: str | None) -> None:
        """Команда-ошибка по единому правилу (§3.3). Одна строка одним Static;
        подсказка closest-match — deep-dim в той же строке."""
        from rich.text import Text
        line = Text()
        line.append(f"[!] неизвестная команда: /{name}", style=_ERR_RED)
        if suggestion:
            line.append(f" (вы имели в виду /{suggestion}?)", style=_DEEP_DIM)
        self._trace_mount(Static(line, classes="line-error", markup=False))

    def _last_trace_row_content(self) -> str | None:
        """Содержимое последней строки трейса или None (трейс пуст / не смонтирован)."""
        from tui.widgets.trace_row import TraceRow
        try:
            trace = self.query_one("#trace", VerticalScroll)
        except Exception:
            return None
        # Смотрим ровно ПОСЛЕДНЕГО ребёнка (дедуп CTA сравнивает с ним): цикл «иду с
        # конца» обещал бы поиск, которого нет — оба его пути выходили на первом шаге.
        kids = trace.children
        last = kids[-1] if kids else None
        return getattr(last, "_content", None) if isinstance(last, TraceRow) else None

    def _mount_trace_row(
        self,
        *,
        prefix: str,
        content: str,
        kind_class: str,
        event_no: int = 0,
        focusable: bool = True,
    ) -> "TraceRow":
        """Смонтировать строку трассы через TraceRow (Horizontal prefix + content).

        focusable=True (дефолт) — для самостоятельных одно-строчных событий.
        focusable=False — для continuation/декорация-строк, уже фокусируемых
        главным виджетом события (например, `* note:` под submit_solution)."""
        from tui.widgets.trace_row import TraceRow
        row = TraceRow(
            prefix=prefix, content=content, kind_class=kind_class, event_no=event_no,
            focusable=focusable,
        )
        self._trace_mount(row)
        return row
