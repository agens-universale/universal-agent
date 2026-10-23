"""TraceScreen — главный экран: трейс событий, инпут с палитрой команд и
пикерами (skill/arg/file), ask_human-баннер, pending-реплики и polling треда."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import threading
import time
import uuid
from collections import namedtuple
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Optional

if TYPE_CHECKING:
    from gc_core.vision.notes import TraceNote

    from gc_tui.widgets.user_reply import UserReplyContainer

from gc_core.attachments.model import Attachment, attachments_of
from gc_core.attachments.numbering import read_image_seq
from gc_core.config_modules.agent_edits import (
    DEFAULT_TURN_SUMMARY_IN_STATUS,
)
from gc_core.config_modules.images import read_images_config
from gc_core.intent_log.record import ACTION_USER_ASK
from gc_core.intent_log.serialize import deserialize_list
from gc_core.message_policy.plan_messages import accepted_plan_messages
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)
from gc_core.persistence.turn_outcome import (
    TurnOutcome,
    is_turn_outcome_record,
    parse_turn_outcome,
)
from gc_core.plan_store import plan_awaits_decision, plan_is_accepted
from gc_core.runtime_notices import collect_unread_runtime_notices
from gc_core.security.permission_mode import Mode, mode_label
from gc_core.tools.skills.skill_arg_resolver import (
    Failed,
    Resolved,
    ResolveResult,
    catalog_from_entries,
    resolve_skill_exact,
    suggest_skills,
)
from gc_core.utils.path_truncate import truncate_path
from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Static, TextArea

from gc_tui import branding, textual_compat
from gc_tui.commands import build_default_registry, closest_command
from gc_tui.messages import (
    BlockToggle,
    CopyBlockRequest,
    OpenImageRequest,
    ScrollToEnd,
    ScrollToReply,
)
from gc_tui.models import (
    CompactionStep,
    ResultOutcome,
    TaskCursor,
)
from gc_tui.palette import ACCENT as _ACCENT
from gc_tui.palette import DEEP_DIM as _DEEP_DIM
from gc_tui.palette import DIM as _DIM
from gc_tui.palette import ERR as _ERR_RED
from gc_tui.palette import INK as _INK
from gc_tui.palette import OK as _OK
from gc_tui.palette import WARN as _WARN
from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
from gc_tui.services import image_hints, image_paste, os_ops
from gc_tui.services.answer_flow import AnswerFlow
from gc_tui.services.block_edits import (
    RanResult,
    call_edit,
    edit_settings,
    ran_result_of,
)
from gc_tui.services.clipboard_ops import copy_out
from gc_tui.services.command_output import split_command
from gc_tui.services.context_meter import render_context_meter
from gc_tui.services.export import agent_answers, export_thread
from gc_tui.services.footer_slot import (
    COPY_DONE_LAST,
    COPY_DONE_NTH,
    COPY_REFUSAL_BAD_N,
    COPY_REFUSAL_EMPTY,
    COPY_REFUSAL_FAILED,
    COPY_REFUSAL_TOO_BIG,
    HINT_TEXT,
    IMAGE_OPEN_TIP,
    IMAGE_PASTE_BUSY_HINT,
    IMAGE_PASTE_OWN_HINTS,
    IMAGE_PASTE_REFUSALS,
    IMAGE_PASTE_WAIT_HINT,
    IMAGE_REFUSAL_EMPTY_CLIPBOARD,
    IMAGES_WITH_COMMAND_REFUSAL,
    NESTED_NO_DETAIL,
    PASTE_HINT_TEXT,
    QUEUE_ADMITTED_HINT,
    RETENTION_DETAIL_HINT,
    toggle_info_line,
)
from gc_tui.services.git_info import current_branch
from gc_tui.services.image_chips import strip_image_chips
from gc_tui.services.image_open import open_image
from gc_tui.services.mode_pill import (
    MODE_HINTS,
    PILL_GAP,
    mode_hint_rows,
    render_mode_pill,
)
from gc_tui.services.notices_ui import format_notices_badge
from gc_tui.services.reasoning_clock import COGNITIVE_TOOLS, ReasoningClock
from gc_tui.services.session_lock import (
    BUSY_MESSAGE,
    heartbeat,
    release,
    try_acquire,
)
from gc_tui.services.session_switch import (
    RUN_SWITCH_HINT,
    RUN_SWITCH_MESSAGE,
    RUN_SWITCH_TITLE,
)
from gc_tui.services.session_title import (
    LAST_ACTUAL_MIN,
    display_name,
    normalize_prompt_title,
    should_update_task,
    task_title,
)
from gc_tui.services.skill_view import skill_input_name
from gc_tui.services.stores.file_store import FileTaskStore
from gc_tui.services.subagent_progress import (
    SubagentProgressConsumer,
    first_pending,
    format_parent_live_status,
)
from gc_tui.services.subagent_sidecar_mapper import (
    CHILD_SUMMARY_LABEL,
    apply_sidecar_nested_actions,
    nested_raw_texts,
    nested_result_event,
    plan_sidecar_nested_actions,
    resolve_nested_tool_detail,
    trace_events_from_nested_detail,
)
from gc_tui.services.terminal_title import (
    format_terminal_title,
    set_terminal_title,
)
from gc_tui.services.tool_blocks import (
    BlockEdit,
    BlockKind,
    CallTimers,
    ToolBlocks,
    block_kind,
)
from gc_tui.services.trace_mapper import (
    PROPOSE_PLAN_ID,
    RenderContext,
    coerce_args,
    collect_render_context,
    map_message,
    response_tail,
    with_closing_replies,
)
from gc_tui.services.trace_render import (
    build_call_row,
    build_compaction_marker_event,
    build_compaction_widgets,
    build_nested_prose_row,
    build_turn_line,
    level_prefix,
    tool_caption,
)
from gc_tui.services.trace_retention import (
    apply_payload_retention,
    events_leaving_window,
    filter_sidecar_to_parents,
    is_light_payload,
    message_ids_in_retention_window,
    nested_call_parent_id,
    parent_outside_retention,
    retention_window_start_index,
    subagent_parent_ids_from_events,
)
from gc_tui.services.trace_scroll import reply_above
from gc_tui.services.turn_outcome import (
    kind_of,
    meta_status_of,
    outcome_error,
    outcome_word,
    settled_status,
    word_of_status,
)
from gc_tui.services.usage_meter import (
    compose_adaptive_footer_right,
    usage_segments,
)
from gc_tui.services.voice_pill import render_pill
from gc_tui.session_settings import read_breaker_from_meta, read_from_meta
from gc_tui.toggles import (
    AUTO_LEVEL_VALUE,
    PROFILE_NONE_VALUE,
    TOGGLES,
    apply_toggle,
    dynamic_toggle_hint,
    effective_toggle_values,
    next_toggle_value,
    preset_custom_hint,
    preset_track_extra,
    toggle_state,
    toggle_unavailable_hint,
)
from gc_tui.widgets.click_model import focus_target_at
from gc_tui.widgets.command_input import CommandTextArea
from gc_tui.widgets.command_palette import CommandPalette
from gc_tui.widgets.plan_panel import PlanPanel
from gc_tui.widgets.static_content import text_key, update_if_changed
from gc_tui.widgets.thinking_indicator import ThinkingIndicator
from gc_tui.widgets.tool_blocks_view import (
    BlockViews,
    ToolBlocksView,
    end_turn,
    nested_tail,
)
from gc_tui.widgets.tool_call_row import ToolCallRow
from gc_tui.widgets.trace_overlays import (
    ScrollEndPill,
    StickyReply,
    pill_label,
)
from gc_tui.widgets.trace_row import (
    CompactionBody,
    CompactionRule,
    TraceRow,
)
from gc_tui.widgets.turn_line import (
    TURN_LINE_HELD,
    TURN_LINE_LATEST,
    TurnLine,
)

# Константы вместо повторяющихся литералов (Sonar S1192).
_PREFIX_EXPORTED = "[exported] "
_SEL_CMDINPUT = "#cmdinput"
_SEL_FOOTER_MODE = "#footer-mode"
_SEL_FOOTER_SLOT = "#footer-slot"
_SEL_INDICATOR_BAR = "#indicator-bar"
_SEL_INPUT_ROW = "#input-row"
_SEL_ROOT = "#root"
_SEL_TRACE = "#trace"
# Бар индикатора (S10 §3.3): стоит только во время работы открытого треда,
# высота постоянна — индикатор и нижний ряд (на нём плашка «в конец»).
_BAR_TURN = "-turn"
_BAR_HEIGHT = 2
# Нижний зазор ленты (`margin-bottom` #trace): в `region` ленты не входит.
_TRACE_GAP = 1


_log = logging.getLogger(__name__)

# События, которыми диалог продолжается после вопроса ask_human.
_DIALOG_CONTINUATION_KINDS = frozenset(
    {"user", "agent", "call", "result", "error", "error_msg"}
)


def select_indicator_mode(*, err, is_running, ask_human, confirm):
    """Режим строки-индикатора в _poll_active. Порядок важен.

    Human-wait (ask_human/confirm) выше err: агент ждёт ответ юзера — баннер
    уже говорит об этом; ✕ Ошибка поверх «АГЕНТ СПРАШИВАЕТ» путает (в т.ч. когда
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
    finish_gate_checking=False,
):
    """Под-состояние индикатора в режиме "active". Порядок = приоритет.

    - summarizing (компакция истории) → "compressing" (сжимаю контекст…);
    - finish_gate_checking (судья ответа — turn_finish или текстового —
      реально сейчас вызывается) → "processing" (сверяюсь…) — ВЫШЕ защёлки
      _finalized/finalizing, иначе «завершаю» перебивает и судья работает
      невидимо;
    - finalizing (принят финальный ответ — turn_finish ИЛИ финальная проза —
      идёт запись/саммари/flush/cleanup) → "validating" (завершаю…);
    - иначе → "thinking" (думаю…).

    «сверяюсь…» — только по флагу ядра: тишина после скрытого события
    (техзаметка режима) — обычный вызов модели, не проверка.
    """
    if summarizing:
        return "compressing"
    if finish_gate_checking:
        return "processing"
    if finalizing or last_event_kind == "_finalized":
        return "validating"
    return "thinking"


# Футер с функциональными клавишами; /stop добавляется только при активном run.
# (/skills, /export доступны через палитру — в футере лишние. /mcp не дублируем —
# подсказка «/mcp» уже в бренд-карточке.)
# Diff: F3 в базовом хроме футера; в ленте о правках говорят блоки диффов.
# Отступ колонки статусов от логотипа — тот же, что в CSS (#brand-mcp padding-left):
# порог узости считается по нему, и разъехаться они не должны.
_MCP_COL_PAD = 6
# Слева в футере — пилюля режима разрешений и счётчик уведомлений (решение
# владельца): постоянный индикатор режима важнее списка F-клавиш, а клавиши
# перечислены в справке (F1). Во время хода к пилюле добавляется ТОЛЬКО
# остановка: место нужно правой колонке, там живут стрелка перехода и сегмент
# «запрос».
_FOOTER_RUNNING_LEFT = "Ctrl+C/stop"
_PLACEHOLDER_READY = "опишите задачу или /команда"
# Раз за запуск, до первого действия человека: потом справку он уже знает
# (сообщение, F1, палитра, новая сессия — `spend_help_hint`).
_PLACEHOLDER_READY_HELP = f"{_PLACEHOLDER_READY} · F1 — справка"
_PLACEHOLDER_PLAN_REWORK = "что доработать в плане…"
_PLACEHOLDER_ASK_HUMAN = "ваш ответ агенту…"
_PLACEHOLDER_PLAN_WAITS = "опишите задачу или /plan — вернуться к плану"
# Ответы команды /plan, когда показывать нечего.
_PLAN_CMD_ENTERED = (
    "plan mode включён — опишите задачу, и агент предложит план"
)
_PLAN_CMD_NO_PLAN = "плана пока нет — опишите задачу, и агент предложит план"
_PLAN_CMD_NO_PLAN_BUSY = "плана пока нет"
_PLAN_CLEAR_HINT = "/plan clear — убрать план из сессии"
# Сколько тиков опроса (по 250 мс) пометка вызова ждёт его строку в ленте,
# прежде чем встать отдельной строкой `!`.
_NOTE_OWNER_WAIT_TICKS = 8
# Вставка изображения (спека §12, визуальный контракт §2). «Вставка…» держится до
# результата: погасшая поверх неё подсказка и её собственный таймер возвращают её же
# (`_clear_footer_hint`), снимает её конец вставки; саму вставку ограничивает
# `image_paste.PASTE_TIMEOUT_SECONDS`.
_IMAGE_PASTE_HINT_SECONDS = 30.0
# «Изображения нет» у вставки: текст вместо картинки (путь, текст ОС) или свой буфер
# приложения; Alt+V читает текст ОС вне UI-потока — корутиной.
_OnMiss = Callable[[], Optional[Awaitable[None]]]
# Модель и backend-режим показываются в шапке экрана (_compose_header_segments).

# Лесенка усечения имени профиля в футере (Task 9, docs
# 2026-08-09-profiles-a-migration-plan): потолок пробуется ВСЕГДА, даже когда
# места хватает с избытком — длинное имя не должно отжирать футер; дальше
# ужимается по факту нехватки ширины вплоть до минимума, ниже которого имя
# снимается целиком.
_PROFILE_NAME_MAX_COLS = 14  # потолок: 13 символов + «…»
_PROFILE_NAME_MIN_COLS = 5  # минимум: 4 символа + «…» (Task 17)
# Имя модели в футере перед снятием режется по середине (спека §10.4):
# начало (провайдер) и конец (версия) остаются; ниже минимума не ужимается —
# дальше имя снимается целиком.
_MODEL_NAME_MIN_COLS = 10

# Хром футера: padding #footer (0 1 → 2) + padding-right #footer-right (1) +
# зазор между колонками, который держит padding-right у #footer-left (2). На
# столько ширина всего футера больше суммы контента обеих колонок.
_FOOTER_CHROME_COLS = 5
# Раскладка usage-блока внутри варианта правой колонки: «всё одной строкой» и
# «цены вниз» — не результат подбора ширины, а РАЗНЫЕ ступени лесенки, поэтому
# `compose_adaptive_footer_right` получает не бюджет, а заведомый предел,
# включающий нужную ветку.
_RIGHT_ONE_LINE = 10**6
_RIGHT_PRICES_DOWN = 0
# «Ещё не резолвили» для необязательных аргументов, у которых None — законное
# значение (плашка контекста скрыта).
_UNRESOLVED = object()

# Снимок футер-головы (спека auto-routing §7.1): profile/mode_auto/level_color/
# model/running — mode_auto различает режим (`● auto`) от факта (`● → ● модель`)
# и от ручного уровня; model/level_color — факт бегущего хода (Router executor'а
# по фактическому уровню подготовленного вызова) либо живой резолв витрины. См.
# `TraceScreen._footer_head_snapshot`.
_FooterHeadSnapshot = namedtuple(
    "_FooterHeadSnapshot",
    ("profile", "mode_auto", "level_color", "model", "running", "transition"),
    defaults=(None,),
)

# Данные одной сборки футера, резолвятся ОДИН раз на кадр и идут в обе
# колонки: бейдж уведомлений (строка 2 левой и резерв лесенки правой),
# снимок шапки, плашка контекста и сегменты цен («запрос», «сессия»).
_FooterFrame = namedtuple(
    "_FooterFrame", ("badge", "snapshot", "meter", "usage")
)


def _records_context(records: list[dict], root: str) -> RenderContext:
    """Контекст рендера порции записей треда (по их payload)."""
    return collect_render_context(
        (rec.get("payload") for rec in records if isinstance(rec, dict)),
        root=root,
    )


def _profile_source_label(ref) -> str | None:
    """Откуда пришёл активный профиль: проектный указатель, глобальный или его нет."""
    if ref is None:
        return None
    return "проектный" if ref.root_kind == "project" else "глобальный"


def _fit_cells(text: str, cols: int) -> str:
    """Самое длинное начало `text`, что помещается в `cols` колонок."""
    out = ""
    for ch in text:
        if cell_len(out + ch) > cols:
            break
        out += ch
    return out


def _truncate_profile_name(name: str, max_cols: int) -> str:
    """Режет ХВОСТ имени профиля до `max_cols` колонок (cell-width), с «…» на
    конце, если целиком не влезает."""
    if cell_len(name) <= max_cols:
        return name
    return _fit_cells(name, max_cols - 1) + "…"


def _truncate_middle(name: str, max_cols: int) -> str:
    """Режет СЕРЕДИНУ имени до `max_cols` колонок: начало (провайдер) и
    конец (версия) остаются — `gatewayhub/…-coder-4.5`."""
    if cell_len(name) <= max_cols:
        return name
    budget = max_cols - 1
    head = _fit_cells(name, budget - budget // 2)
    tail = _fit_cells(name[::-1], budget // 2)[::-1]
    return f"{head}…{tail}"


class _TraceBlockHost:
    """Хост блоков верхнего уровня живой ленты (спека P4 §8.1): в конец —
    через `_trace_mount` (правило очереди), рядом с виджетом блока —
    прямой `trace.mount`."""

    def __init__(self, screen: TraceScreen) -> None:
        self._screen = screen

    def mount(self, widget, *, before=None, after=None) -> None:
        self._screen._mount_block_widget(widget, before=before, after=after)


class _NestedBlockHost:
    """Хост блоков уровня субагента: монтирование под строкой `Agent`
    (`_nested_mount_after`), в конец — за последним виджетом уровня."""

    def __init__(self, screen: TraceScreen, parent_call_id: str) -> None:
        self._screen = screen
        self._parent = parent_call_id

    def mount(self, widget, *, before=None, after=None) -> None:
        self._screen._nested_mount_after(
            self._parent, widget, after=after, before=before
        )


class TraceScreen(Screen):
    """Главный экран: трейс, ввод пользователя, команды и сопровождение run'а."""

    # Ctrl+A — select-all в #cmdinput (CommandTextArea); git add всего — только на /diff.
    # Ctrl+V: priority — иначе при фокусе в трейсе (клик по строке во время run)
    # хоткей никуда не доходит: у TraceRow/ToolCallRow нет action_paste.
    # Alt+V — изображение из буфера (на Windows Ctrl+V забирает сам терминал); та же
    # priority-причина и дубль под русскую раскладку.
    BINDINGS = [
        Binding(
            "ctrl+v", "paste_to_input", "вставить", show=False, priority=True
        ),
        Binding(
            "ctrl+м", "paste_to_input", "вставить", show=False, priority=True
        ),
        Binding(
            "alt+v",
            "paste_image_to_input",
            "вставить изображение",
            show=False,
            priority=True,
        ),
        Binding(
            "alt+м",
            "paste_image_to_input",
            "вставить изображение",
            show=False,
            priority=True,
        ),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._registry = build_default_registry()
        self._palette: CommandPalette | None = None
        self._brand: Vertical | None = None
        self._brand_wide: bool | None = (
            None  # раскладка карточки: статусы сбоку/снизу
        )
        self._brand_row = None  # контейнер карточки (ссылка, не запрос)
        self._task_events: list = []  # list[TraceEvent]; пополняется в _render_record/_render_segments
        # A1: правило показа ответов (дедуп, попытки turn_finish) — то же,
        # что у /copy.
        self._answer_flow = AnswerFlow()
        # Новые реплики агента с ухода от низа — счётчик плашки «в конец».
        self._new_replies = 0
        # Из них — посчитанные попытки turn_finish, которые ещё может убрать
        # свёртка: убранная попытка из счётчика вычитается.
        self._new_attempt_replies = 0
        self._pill_enter = False  # хвост подписи плашки: «или Enter»
        self._copy_seq = 0  # номер последнего /copy: старые итоги глохнут
        self._live_turn_tool_calls: int = (
            0  # живые call-события текущего хода (эвристика имени)
        )
        self._store: Optional[FileTaskStore] = None
        self._cursor: Optional[TaskCursor] = None
        self._next_no: int = 1
        self._active_task_id: Optional[str] = None
        # Снимок меты на тик опроса (см. _tick_meta); вне тика — None.
        self._tick_meta_cache: tuple[str, dict] | None = None
        self._in_poll_tick = False
        self._session_lock_hb_mono: float = 0.0  # throttle heartbeat ≤ 1 Гц
        self._error_shown: bool = False
        # Пометки вызова, чья строка ещё не в ленте: (тред, пометка, тиков
        # ожидания осталось) — см. _drain_trace_notes.
        self._waiting_notes: list[tuple[str | None, TraceNote, int]] = []
        self._mcp_status_row = (
            None  # Optional[Static] — live MCP-loading индикатор
        )
        self._warnings_toasted = (
            False  # warning-тосты при старте показываются один раз за запуск
        )
        self._mcp_status_done = False
        # последняя MCP-строка карточки — для перерисовки (runtime-notice / update-notice)
        self._last_mcp_card_line: Optional[str] = None
        # Phase 2: tool-row mutation state
        self._active_calls: dict[str, ToolCallRow] = {}
        # Блоки вызовов (P4): застывшие таймеры — один реестр на ленту,
        # исполнители моделей — по уровню (`""` — верхний).
        self._call_timers = CallTimers()
        self._block_views = BlockViews()
        # Nested progress под subagent_*: live sink + reopen из data_subagents_*.
        self._subagent_progress = SubagentProgressConsumer()
        self._nested_tool_rows: dict[
            str, ToolCallRow
        ] = {}  # child tool_call_id → row
        self._nested_widgets_by_parent: dict[
            str, list
        ] = {}  # parent_call_id → widgets
        # Durable parent rows for subagent_* (survive pop from _active_calls on result).
        self._subagent_parent_rows: dict[str, ToolCallRow] = {}
        # Подписи MCP для строк вызовов: (ключ источника, wire→инфо,
        # серверы) — пересчёт только при смене источника (`_mcp_tool_info`).
        self._mcp_info_cache: Optional[tuple[tuple, dict, tuple]] = None
        self._reasoning_clock = ReasoningClock()
        # Журнал правок текущего треда для инлайн-диффов: один EditsLookup на
        # тред (внутри — кэш индекса), пересоздаётся при смене идентичности.
        self._edits_lookup = None  # Optional[EditsLookup]
        self._edits_lookup_key: Optional[tuple] = None
        self._submit_solution_call_ids: set[str] = set()
        # Виджеты последней попытки turn_finish хода (ответ, код, итог,
        # заметка) — при повторе свернуть предыдущую.
        self._submit_solution_widgets: list[list[Widget]] = []
        # Записи незаконченного ответа модели из прошлой пачки опроса.
        self._response_tail: list = []
        # event_no → тело сводки (скрыто; раскрывается кликом/Enter метки).
        self._summary_body_widgets: dict[int, CompactionBody] = {}
        self._summary_marker_widgets: dict[int, CompactionRule] = {}
        # Итог, смонтированный при стоящем баре, ждёт его ухода (S10 §3.4):
        # здесь — как раскрыть каждый.
        self._held_reveals: list[Callable[[], None]] = []
        # Phase 5: ask_human banner state
        self._ask_human_banner = None  # type: Optional[AskHumanBanner]
        self._ask_human_call_id: Optional[str] = None
        # confirm banner state (T2)
        self._confirm_banner = None  # type: Optional["ConfirmBanner"]
        self._confirm_active: bool = False
        self._plan_banner = None  # type: Optional["PlanApprovalBanner"]
        # Выбрано «Обсудить и доработать план»: следующее сообщение человека —
        # замечания к плану, о чём в ленте остаётся строка.
        self._plan_rework_pending = False
        # Подсказка F1 в поле ввода погашена до конца запуска. Экран главный и
        # живёт весь запуск — признак принадлежит ему.
        self._help_hint_spent = False
        # Скрытая трейс-строка вопроса; раскрывается при схлопывании баннера.
        self._ask_human_question_row = None  # type: Optional[TraceRow]
        # Раннер, задавший текущий ask_human (владелец вопроса). None, если плашка
        # воспроизведена из истории — тогда ответ идёт новым ходом, а не в stdin.
        self._ask_human_owner_runner = None
        # Child HITL: parent_call_id субагента, пока висит его ask_human-баннер.
        # Ответ → stdin child, без parent pending / user-строки в data_*.
        self._ask_human_child_parent_call_id: Optional[str] = None
        # Spec 2026-05-26: pending user-replies (in-memory dict by client_id, FIFO ordered).
        self._pending_user_widgets: dict[str, UserReplyContainer] = {}
        # client_id реплик, которые стоп или конец хода не сняли у ядра
        # (`_withdraw_queue`): ядро их держит, пока тред их не сверит.
        self._held_client_ids: set[str] = set()
        # id записей, уже отрисованных в трейсе: запись с таким id не
        # рисуется повторно ни на одном пути (rebuild, live-тик).
        self._rendered_ids: set[str] = set()
        # Индекс первого события каждого сегмента в _task_events (retention).
        self._segment_starts: list[int] = []
        # Кэш wire_content для reconciliation — независим от состояния meta-файла,
        # который executor может перезаписывать в своём потоке.
        self._pending_wire_cache: dict[str, str] = {}
        self._skill_picker = None  # type: Optional["SkillPicker"]
        self._arg_picker = None  # type: Optional["ArgPicker"]
        self._file_picker = None  # FilePicker | None
        # Сеанс файлового пикера: индекс путей собирается один раз; достроенный
        # сбрасывается при закрытии пикера, ещё строящийся переживает закрытие и
        # подхватывается следующим сеансом (см. _close_file_picker). _file_query —
        # последний запрошенный query (None = пикер не нужен). Поиск по индексу считает
        # один фоновый поток сеанса (_file_search). Опоздавший результат отбрасывается
        # по паре «тот же объект индекса + тот же query»: сам по себе объект индекса
        # сеансы НЕ различает.
        self._path_index = None  # file_index.PathIndex | None
        self._file_query: Optional[str] = None
        self._file_shown_query: Optional[str] = (
            None  # для какого query показан список
        )
        self._file_search = None  # file_search.FileSearchSession | None
        self._skills_cache_value: Optional[list[dict]] = None
        # После Tab-completion в skill picker гасим один Changed-цикл, чтобы
        # on_text_area_changed не переоткрыл только что закрытый picker.
        self._suppress_picker_reopen: bool = False
        # Spec 2026-05-26: ThinkingIndicator state.
        self._thinking = None  # type: Optional["ThinkingIndicator"]
        self._trace_max_height = None  # type: Optional[int]
        self._stick_bottom = (
            True  # автоскролл-намерение «липнуть к низу» трейса
        )
        self._last_auto_y = 0  # куда мы в прошлый раз программно прижали трейс
        # Кэш реплик ленты для липкой реплики (_trace_replies).
        self._trace_replies_key: Optional[tuple] = None
        self._trace_replies_cache: list = []
        # Freeze UI на время app-select (§3.2 F1): time.monotonic() последней
        # активности выделения (сторож F5); None когда не selecting.
        self._select_freeze_since: Optional[float] = None
        # Эфемерный StreamBlock — live-вывод текущего chat_completions-шага;
        # снимается, когда приходит каноничная assistant-строка / run завершён.
        self._stream_block = None  # type: Optional["StreamBlock"]
        self._stream_answer_row = None  # type: Optional["TraceRow"]
        # Кумулятивный таймер «думаю…» — стартует на каждом user-send,
        # передаётся в виджет, чтобы elapsed не сбрасывался при show/hide циклах.
        self._thinking_started_at: Optional[float] = None
        # Вид последнего события ленты; «_finalized» — финальный ответ виден
        # (индикатор «завершаю…»).
        self._last_event_kind: Optional[str] = None
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
        # (Раньше второй источник давал прилипание/устаревание.)
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
        # Реплики, снятые откатом отправки, по тредам: запись итога их хода
        # остановленный поток может дописать позже (висит в повторах HTTP) —
        # экран её не показывает, чистка лишь снимает её из файла. Живёт до
        # чистки сирот треда, а не до смены треда: ушёл и вернулся, пока
        # поток висит, — запись в файле ещё может лечь.
        self._undone_reply_ids: dict[str, set[str]] = {}
        # Флаг состояния runner на предыдущем тике — для детекта спадающего фронта.
        self._was_running: bool = False
        # Прогон, чей исход ещё не записан: раннер, которого тик застал
        # бегущим, и отметка «ход запущен отсюда» (быстрый ход тик может не
        # застать). Раннер держим сами: стоп и пересборка снимают его с app.
        self._watched_runner = None
        # Номер прогона, который тик застал бегущим (`AgentRunner.run_seq`):
        # сменился при «всё ещё бежит» — хвост стартовал между тиками.
        self._watched_run_seq: Optional[int] = None
        self._run_pending: bool = False
        # Зафиксированный прогон — пара (раннер, `run_seq`): его конец уже
        # записан, бар ему не нужен (S10 §3.7).
        self._settled_run: Optional[tuple] = None
        # Снимок `tui` открытого треда до записи «running» новым ходом —
        # `(тред, {status, last_indicator})`: откат его возвращает (§3.8).
        self._pre_run_tui_meta: Optional[tuple[str, dict]] = None
        # Запись итога последнего хода открытого треда (из потока записей)
        # и признак «после неё началась реплика» — тогда последний ход
        # записи не имеет, строка — по статусу сессии.
        self._last_turn_outcome: Optional[TurnOutcome] = None
        self._outcome_superseded: bool = False
        # Сводка правок закрытого хода открытого треда (`_close_turn_edits`):
        # строка итога получает её, когда встанет, — запись стопа может
        # дочитаться позже спадающего фронта.
        self._closed_turn_summary: Optional[tuple] = None
        # Остановленный раннер, чей поток ещё дорабатывает (стоп
        # кооперативный): строка итога мигает «■» (решение 26).
        self._stopping_runner = None
        self._was_summarizing: bool = (
            False  # для baseline last_compact_msg_count
        )
        # Последний ход журнала правок на старте прогона — база для сводки хода.
        self._edits_baseline_turn_id: Optional[str] = None
        # Граница хода снята для ТЕКУЩЕГО прогона: отметку старта ставят
        # дважды (подготовка и страховочная сетка), и вторая не вправе её
        # двигать. Снимается на спадающем фронте.
        self._edits_baseline_taken = False
        # Транзиентная подсказка в слоте футера (строка 2: esc-confirm,
        # paste-hint, отказы и советы делят его).
        self._esc_confirm_kind: Optional[str] = (
            None  # "clear" | "stop" | "paste" | None
        )
        self._footer_hint_timer = None  # textual.Timer
        self._footer_hint_active: bool = False
        self._footer_hint_text: Optional[str] = (
            None  # текст активной подсказки
        )
        # Активная подсказка — пояснение режима: занимает обе строки левой
        # части. Shift+Tab во время записи откладывает пояснение до её конца.
        self._footer_mode_hint: Optional[str] = None
        self._mode_hint_after_voice: Optional[str] = None
        self._footer_refusal_active: bool = False
        # Цвет активного отклика слота: "warn" — отказ, "ok" — успех, None —
        # обычная подсказка.
        self._footer_hint_tone: Optional[str] = None
        self._refusal_run_id = None  # прогон, ШЕДШИЙ при постановке отказа
        self._refusal_input_text = ""  # текст поля ввода при постановке отказа
        # Ключ последней сборки футера (`_footer_inputs`); None — не собран.
        self._footer_key: Optional[tuple] = None
        # Части футера, чей сбой уже в логе: пишем раз на эпизод,
        # не каждый тик.
        self._footer_faults: set[str] = set()
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
        # Итог доставки скилла (id, загружен): ставится в
        # _consume_pending_prefix, забирается pending-репликой
        # (_mount_user_reply) — имя в реплике, «не найден» под ней.
        self._pending_skill_marker: Optional[tuple[str, bool]] = None

        # Голосовой ввод (спека docs/tui/specs/2026-07-05-voice-input-design.md):
        # flow собирается лениво на первый Ctrl+T; таймер листает кадры пилюли.
        self._voice = None  # type: Optional["VoiceInputFlow"]
        self._voice_timer = None  # textual.Timer | None
        self._voice_frame: int = 0
        # Конфиг перечитали посреди записи: рвать её нельзя, а кэш flow уже протух —
        # снимем его, когда запись кончится (см. config_reloaded / _on_voice_state).
        self._voice_flow_stale: bool = False
        # Идёт проверка живости батарейки или её подъём (воркеры Ctrl+T): второй такой
        # же по повторному нажатию не заводим.
        self._voice_service_pending: bool = False
        # Вставка изображения (спека §12): чтение буфера/файла и нормализация — воркером
        # вне UI-потока, по одной; пути и Ctrl+V с текстом, пришедшие во время чтения,
        # ждут в очереди (_image_paste_queue: путь или None — буфер, что сделать, если
        # это не картинка, и «только скопированные файлы»). Пока идёт
        # вставка с очередью, Enter не отправляет. _image_seq_issued —
        # тред → последний номер image-плашки, выданный в этом процессе (None — ещё не
        # созданный тред): сообщение с картинкой могло ещё не дойти до meta треда
        # (runtime.image_seq), а номер уже занят — и вернуться в тред посреди хода.
        self._image_paste_busy: bool = False
        self._image_paste_worker = None  # textual.worker.Worker | None
        self._image_paste_queue: list[tuple[Optional[str], _OnMiss, bool]] = []
        self._image_seq_issued: dict[Optional[str], int] = {}
        # Подсказки картинок (контракт картинок §2). Метки буфера обмена: последний
        # проверенный буфер с картинкой и тот, о котором уже подсказали или из которого
        # уже вставили, — о нём совет больше не звучит. Совет про Ctrl+клик — раз за запуск.
        self._clipboard_stamp: Optional[str] = None
        self._clipboard_tip_stamp: Optional[str] = None
        self._image_open_tip_shown: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            left_text, right_text = self._compose_header_segments()
            with Horizontal(id="header"):
                yield Static(left_text, id="header-left", markup=False)
                yield Static(right_text, id="header-right", markup=False)
            trace = VerticalScroll(id="trace")
            yield trace
            yield Vertical(id="indicator-bar")
            with Horizontal(id="input-row"):
                yield Static("› ", id="input-prompt", markup=False)
                inp = CommandTextArea(
                    placeholder=_PLACEHOLDER_READY_HELP, id="cmdinput"
                )
                inp.is_known_command = self._is_known_command
                yield inp
            with Horizontal(id="footer"):
                with Vertical(id="footer-left"):
                    yield Static("", id="footer-mode", markup=False)
                    yield Static("", id="footer-slot", markup=False)
                yield Static(
                    self._compose_footer_right(),
                    id="footer-right",
                    markup=False,
                )
        yield StickyReply(trace, id="trace-sticky-reply")
        yield ScrollEndPill(trace, id="trace-end-pill")

    def _compose_header_segments(self) -> tuple[str, str]:
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
        self._refresh_header()  # пересчитать усечение пути под новую ширину
        self._refresh_footer()  # адаптивная вёрстка usage (стек сессии) под новую ширину
        self._sync_trace_max_height()  # потолок #trace под новую высоту экрана
        # Раскладка бренд-карточки выбиралась только при сборке: сузив окно уже после
        # старта, человек получал колонку статусов в пару символов шириной — слова
        # рвались по буквам («подк / люче / ны»), а совсем узко она схлопывалась в ноль.
        self._apply_brand_layout(self._brand_fits_beside(event.size.width))
        self.call_after_refresh(self._refresh_trace_overlays)

    def on_click(self, event: events.Click) -> None:
        """Клик в ЛЮБОЙ части трейса → фокус на #trace, чтобы колесо/стрелки/PgUp-
        PgDn скроллили его (а не зависели от попадания в пустой край). Сюда доходит
        только клик по пустому месту / не-focusable строке: focusable строки сами
        останавливают событие в своём on_click (self.focus() + event.stop()).
        Клик вне трейса (инпут/футер) фокус трейса не трогает."""
        try:
            trace = self.query_one(_SEL_TRACE, VerticalScroll)
        except Exception:
            return
        if not trace.has_focus and trace.region.contains(
            event.screen_x, event.screen_y
        ):
            trace.focus()

    def get_focusable_widget_at(self, x: int, y: int) -> Optional[Widget]:
        """Кого фокусировать нажатием мыши — с оглядкой на `FOCUS_ON_CLICK`.

        Обход предков в реализации Textual сквозь нефокусируемый блок проходит
        насквозь и приводит к контейнеру #trace: клик по инлайн-диффу утаскивал
        фокус туда и сбивал позицию Tab. Правило — в `click_model`, здесь только
        подключение."""
        return focus_target_at(
            self, x, y, super().get_focusable_widget_at(x, y)
        )

    def action_paste_to_input(self) -> None:
        """Ctrl+V → всегда в #cmdinput, даже если фокус ушёл в трейс (клик/Tab
        во время run). Иначе хоткей «мёртв»: у строк трейса нет action_paste."""
        try:
            inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
        except Exception:
            return
        if inp.disabled:
            return
        if not inp.has_focus:
            inp.focus()
        inp.action_paste()

    def action_paste_image_to_input(self) -> None:
        """Alt+V: изображение из буфера обмена; нет — текст; нет ничего — отказ
        (контракт §2). Как Ctrl+V — всегда в #cmdinput, даже если фокус в трейсе."""
        try:
            inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
        except Exception:
            return
        if inp.disabled:
            return
        if not inp.has_focus:
            inp.focus()
        self._start_image_paste(
            path=None,
            strict=True,
            on_miss=partial(self._apaste_os_text_or_refuse, inp),
        )

    async def _on_paste(self, event: events.Paste) -> None:
        """Bracketed-paste при фокусе не в инпуте (трейс) → вставить в #cmdinput.

        Иначе events.Paste уходит в TraceRow/VerticalScroll и пропадает."""
        focused = self.focused
        if isinstance(focused, CommandTextArea):
            return  # CommandTextArea._on_paste сам разберёт
        try:
            inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
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
        витрин строится от `effective_settings()` на каждый вызов, §6.4).

        Легаси-плейсхолдер шаблона (Task 14) — не имя модели, показывать нечего."""
        from gc_core.onboarding import MODEL_SENTINEL

        display_router = getattr(self.app, "display_router", None)
        if not callable(display_router):
            model = self.app.base_settings().settings.agent.model
            return model if model and model != MODEL_SENTINEL else "?"
        # check_breaker=False: витрина показывает модель честно даже при открытом
        # активном алиасе (спека §12.2) — иначе футер молча замирал бы (§8).
        resolved = display_router().resolve("main", check_breaker=False)
        model = resolved.alias or resolved.model
        return model if model and model != MODEL_SENTINEL else "?"

    def _footer_live_level(self) -> str:
        """Активный уровень ЖИВОГО резолва (спека §12.1/§12.2): реагирует на
        /level и активацию профиля сразу, без пересборки executor. Общий
        источник для цвета будущей модели и сравнения «уровень менялся?»
        посреди вызова; факт берётся через Router работающего executor."""
        display_router = getattr(self.app, "display_router", None)
        return (
            display_router().active_level()
            if callable(display_router)
            else "default"
        )

    def _level_color(self, level: str) -> str:
        """Цвет точки-индикатора для конкретного уровня — из ЕДИНОГО источника
        `TOGGLES["level"]` (спека §12.1): не хардкод, будущий авто-режим со
        своим цветом подхватится сам."""
        colors = {v.value: v.color for v in TOGGLES["level"].values}
        return colors.get(level, _INK)

    def _footer_profile_name(self) -> str | None:
        """Имя активного профиля для футера, или None — тот же источник, что
        `/config`/`/status` (`app.active_profile_ref`)."""
        ref_fn = getattr(self.app, "active_profile_ref", None)
        ref = ref_fn() if callable(ref_fn) else None
        return ref.name if ref is not None else None

    def _footer_running_resolution(self) -> tuple[str, str, str, bool] | None:
        """Факт подготовленного вызова через Router работающего executor.

        Новый выбор уровня или профиля не подменяет этот комплект. До первого
        решения используется живой резолв того же Router. Чтение не строит executor.
        """
        runner = getattr(self.app, "active_runner", None)
        is_running = getattr(runner, "is_running", None)
        if runner is None or not callable(is_running) or not is_running():
            return None
        router_fn = getattr(runner, "executor_router", None)
        router = router_fn() if callable(router_fn) else None
        if router is None:
            return None
        from gc_core.model_router import AliasCircuitOpenError
        from gc_core.onboarding import MODEL_SENTINEL

        call_level = getattr(runner, "call_level", None)
        prepared_level = call_level() if callable(call_level) else None
        level = prepared_level or router.active_level()
        try:
            resolved = router.resolve_main_at_level(level, check_breaker=False)
        except AliasCircuitOpenError as exc:
            resolved = exc.resolved
        except ValueError:
            return None
        model = resolved.alias or resolved.model
        if not model or model == MODEL_SENTINEL:
            return None
        return (
            level,
            self._level_color(level),
            model,
            prepared_level is not None,
        )

    def _footer_head_snapshot(self) -> _FooterHeadSnapshot:
        """Один снимок на кадр: фактический вызов и выбранный следующий режим."""
        profile = self._footer_profile_name()
        running = self._run_in_progress()
        auto_mode_active = getattr(self.app, "auto_mode_active", None)
        selected_auto = (
            auto_mode_active() if callable(auto_mode_active) else False
        )
        resolution = self._footer_running_resolution()
        next_selection = None
        if resolution is not None:
            selection = getattr(
                self.app.active_runner, "next_call_selection", None
            )
            next_selection = selection() if callable(selection) else None
            if next_selection is not None:
                selected_auto = next_selection[0]
        transition = None
        mode_auto = selected_auto
        if resolution is not None:
            level, color, model, prepared = resolution
            runner = self.app.active_runner
            call_mode = getattr(runner, "call_mode", None)
            actual_mode = call_mode() if callable(call_mode) else None
            if actual_mode is not None:
                mode_auto = actual_mode == "auto"
            if selected_auto:
                if actual_mode == "manual":
                    transition = (AUTO_LEVEL_VALUE.color, "auto")
            else:
                if next_selection is None:
                    live_level = self._footer_live_level()
                    live_model = str(self._footer_model_name())
                else:
                    _, live_level, live_model = next_selection
                if prepared and (
                    model != live_model
                    or level != live_level
                    or actual_mode == "auto"
                ):
                    transition = (
                        self._level_color(live_level)
                        if level != live_level or mode_auto
                        else None,
                        live_model,
                    )
        else:
            color = self._level_color(self._footer_live_level())
            model = str(self._footer_model_name())
        return _FooterHeadSnapshot(
            profile, mode_auto, color, model, running, transition
        )

    def _footer_head(
        self,
        *,
        with_profile: bool,
        profile_name_cols: int = _PROFILE_NAME_MAX_COLS,
        snapshot: _FooterHeadSnapshot | None = None,
        with_model_name: bool = True,
        with_level_block: bool = True,
        with_transition: bool = True,
        meter: Text | object | None = _UNRESOLVED,
        model_cols: int | None = None,
    ) -> Text:
        """head = [профиль ] ●[ auto | → ● модель | модель][ · контекст]. Правило
        единое для обоих режимов (спека auto-routing §7.1): нет активного хода —
        точка режима + режим (` auto` в auto, модель в ручном); идёт ход —
        точка режима + факт (` → ● модель` в auto, просто модель в ручном).
        `transition` отдельно называет выбранный следующий режим/модель,
        не подменяя уже подготовленный вызов. Точка режима — жирная, ВСЕГДА первая и видна: в auto
        цветом BRAND (`AUTO_LEVEL_VALUE.color`, единственный источник), иначе
        цветом уровня.

        Порядок жертв по ширине (спека auto-routing §7.1): текст после
        последней точки снимается первым (имя модели или слово `auto`,
        `with_model_name=False`), затем у auto — стрелка и точка уровня
        (`with_level_block=False`); точка режима остаётся всегда.

        Имя профиля режется ХВОСТОМ до `profile_name_cols` колонок (Task 9):
        потолок — `_PROFILE_NAME_MAX_COLS`, чтобы длинное имя не отжирало
        футер даже на широком экране.

        `snapshot` — готовый результат `_footer_head_snapshot()`; не передан —
        резолвим сами (одиночный вызов вне лесенки).

        `meter` — готовая плашка контекста (`None` = скрыта); не передана —
        резолвим сами. Как и `snapshot`, это ОДИН резолв на кадр: метр читает
        meta треда с диска, а лесенка строит голову десятки раз.

        `model_cols` — потолок имени модели, режется по середине (ступень
        перед снятием имени); None — целиком."""
        if snapshot is None:
            snapshot = self._footer_head_snapshot()
        if meter is _UNRESOLVED:
            meter = self._context_meter()
        profile, mode_auto, level_color, model_name, running, transition = (
            snapshot
        )
        head = Text()
        if with_profile and profile:
            head.append(
                _truncate_profile_name(profile, profile_name_cols), style=_INK
            )
            head.append(" ")
        shown = (
            model_name
            if model_cols is None
            else _truncate_middle(model_name, model_cols)
        )
        if mode_auto:
            head.append("●", style=f"bold {AUTO_LEVEL_VALUE.color}")
            if running:
                if with_level_block:
                    head.append(" → ", style=_DIM)
                    head.append("●", style=f"bold {level_color}")
                    if with_model_name:
                        head.append(" ")
                        head.append(shown, style=_INK)
            elif with_model_name:
                head.append(" auto", style=_INK)
        else:
            head.append("●", style=f"bold {level_color}")
            if with_model_name:
                head.append(" ")
                head.append(shown, style=_INK)
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

    def _compose_footer_right(self, frame: _FooterFrame | None = None) -> Text:
        """Правый край футера: [профиль ]●[ модель] · контекст [+ цены вниз].

        Единая лесенка жертв; первое подошедшее и есть ответ:

        1. всё одной строкой справа, пилюля режима слева полная, имя профиля
           в потолке;
        2. цены («запрос · сессия») уезжают на 2-ю строку справа, контекст
           остаётся наверху;
        3. пилюля режима слева сжимается до глифа (режим остаётся виден);
        4. имя профиля ужимается `_PROFILE_NAME_MAX_COLS` → `_MIN_COLS`;
        5. имя профиля снимается целиком;
        6. имя модели ужимается по середине до `_MODEL_NAME_MIN_COLS`;
        7. снимается имя модели (или слово `auto`), затем у auto — стрелка с
           точкой уровня; затем цены — «запрос», потом целиком. Точка
           режима/уровня и плашка контекста остаются всегда.

        Ступени 1-2 ничего не теряют: вторая строка правой колонки оплачена
        высотой футера. Потеря начинается с 3-й, поэтому перебор имени живёт
        целиком в проходе с глифом.

        Ширину правая колонка ЗАЯВЛЯЕТ: #footer-right — width: auto,
        #footer-left — 1fr, остаток достаётся левой. Вариант подходит, если в
        остатке целиком встаёт строка 1 левой (режим + хвост команд — теми же
        `_footer_mode_variants`, что её рисуют) и бейдж уведомлений строки 2.
        Подсказки слота в отбор не входят: длинные гаснут многоточием.

        Лесенка монотонна по содержимому: при сужении окна цены, имя профиля и
        имя модели не возвращаются. Поэтому «всё одной строкой» пробуется лишь
        рядом с полной пилюлей, а перебор имени начинается с потолка.

        Данные кадра (`frame`, `_footer_frame`) резолвятся ОДИН раз (perf) —
        лесенка получает их готовыми и заново не резолвит. Не передан —
        резолвим сами (одиночный вызов вне сборки футера)."""
        if frame is None:
            frame = self._footer_frame()
        request_seg, session_seg = frame.usage
        available = self._footer_right_available_width()
        total = self._footer_total_width()
        snapshot = frame.snapshot
        meter = frame.meter

        def build(
            profile_cols: int | None,
            layout_width: int,
            *,
            with_model_name: bool = True,
            with_level_block: bool = True,
            with_request: bool = True,
            with_prices: bool = True,
            with_transition: bool = True,
            model_cols: int | None = None,
        ) -> Text:
            pending_transition = snapshot.transition is not None
            head = self._footer_head(
                with_profile=profile_cols is not None,
                profile_name_cols=profile_cols or _PROFILE_NAME_MAX_COLS,
                snapshot=snapshot,
                with_model_name=with_model_name or pending_transition,
                with_level_block=with_level_block,
                with_transition=with_model_name and with_transition,
                meter=meter,
                model_cols=model_cols,
            )
            request = request_seg if (with_prices and with_request) else None
            session = session_seg if with_prices else None
            if request is None and session is None:
                return head
            return compose_adaptive_footer_right(
                head, request, session, layout_width
            )

        def width_of(result: Text) -> int:
            return max(cell_len(line) for line in result.plain.split("\n"))

        # Строка 1 меряется в двух формах пилюли: полная — мерка бесплатных
        # ступеней, глиф — платных и пол: «Ctrl+C/stop» и ответ агенту урезать
        # нечем. Пол и бейдж резервируются, дальше жертвует правая.
        full_line, glyph_line = self._footer_mode_variants()
        badge_cols = self._footer_badge_line(frame.badge).cell_len

        def fits(result: Text, mode_line: Text) -> bool:
            need = width_of(result)
            if result.plain.count("\n") > 1 or need > available:
                return False
            left = total - need - _FOOTER_CHROME_COLS
            return left >= max(mode_line.cell_len, badge_cols, 1)

        prices_down = [
            (cols, _RIGHT_PRICES_DOWN)
            for cols in range(
                _PROFILE_NAME_MAX_COLS, _PROFILE_NAME_MIN_COLS - 1, -1
            )
        ]
        # Бесплатный проход — ступени 1-2 рядом с полной пилюлей; платный —
        # рядом с глифом: перебор имени 14 → 5 и его снятие.
        passes = (
            (
                full_line,
                [
                    (_PROFILE_NAME_MAX_COLS, _RIGHT_ONE_LINE),
                    (_PROFILE_NAME_MAX_COLS, _RIGHT_PRICES_DOWN),
                ],
            ),
            (glyph_line, [*prices_down, (None, _RIGHT_PRICES_DOWN)]),
        )
        for mode_line, variants in passes:
            for cols, layout_width in variants:
                result = build(cols, layout_width)
                if fits(result, mode_line):
                    return result

        # Резерв (рунги шапки × до 3 уровней цен), от богатого к бедному:
        # имя модели ужимается по середине, затем снимается (или слово
        # `auto`), затем у auto — стрелка с точкой уровня, затем «запрос»
        # (сессия остаётся), затем цены целиком. Точка режима (в ручном —
        # точка уровня) и плашка контекста остаются всегда.
        #
        # Почему цены вообще стали жертвой (решение владельца, синк 2026-08-20):
        # раньше последняя ступень возвращалась БЕЗУСЛОВНО, и на узком окне левой
        # доставалось меньше нуля колонок — «Ctrl+C/stop» рвался пополам или исчезал
        # совсем. Цену прогона можно посмотреть и потом, а остановить агента больше
        # нечем, поэтому обмен идёт в эту сторону.
        #
        # И почему берётся не первый попавшийся: когда цены стоят на второй строке,
        # ширину правой колонки диктуют ОНИ, а не шапка, — тогда снятие имени профиля
        # и имени модели не освобождает ни колонки. Поэтому если место не нашлось ни на
        # одной ступени, берётся вариант с МИНИМАЛЬНОЙ шириной, а среди равных по
        # ширине — первый, то есть самый богатый: жертва принимается только когда
        # она что-то даёт.
        # Ужатие шапки перебирается ЗАНОВО на каждом уровне цен: снятая строка
        # «запрос» освобождает больше, чем имя профиля, и после неё имя часто
        # возвращается на место. Перебирать в другом порядке значило бы отдавать
        # имя навсегда ради экономии, которой хватило бы и без него.
        # Последняя ступень (`with_level_block=False`) влияет только на auto:
        # в ручном режиме `_footer_head` игнорирует этот флаг вовсе.
        # Рунг: (имя профиля, имя модели, блок уровня, переход, ширина имени).
        # Перед снятием имени — снятие перехода при полном имени, затем ужатие
        # имени по середине (спека §10.4): провайдер и версия видны, пока
        # влезают. В auto без хода на месте имени слово `auto` — ужимать
        # нечего.
        full = cell_len(snapshot.model)
        least = min(full, _MODEL_NAME_MIN_COLS)
        name_shown = snapshot.running or not snapshot.mode_auto
        squeeze = range(full - 1, least - 1, -1) if name_shown else ()
        # Имя ждущего перехода не снимается (`build`), поэтому ступени
        # «снять имя» тогда — одна: имя ужато до минимума, снят блок уровня
        # (в auto это «→ ●»). Без неё самой узкой осталась бы ступень с
        # блоком, и на узком окне правая поджимала бы левую.
        drop_name = (
            ((None, False, False, False, least),)
            if snapshot.transition is not None
            else (
                (None, False, True, False, None),
                (None, False, False, False, None),
            )
        )
        head_ladder = (
            (_PROFILE_NAME_MAX_COLS, True, True, True, None),
            (None, True, True, True, None),
            (None, True, True, False, None),
            *((None, True, True, False, c) for c in squeeze),
            *drop_name,
        )
        # Ценами жертвуем ТОЛЬКО ради пола левой колонки (глиф режима,
        # критичные клавиши и бейдж уведомлений).
        price_ladder = ({}, {"with_request": False}, {"with_prices": False})
        # Лениво: до хвоста резерва доходит только узкое окно.
        reserve = (
            build(
                cols,
                _RIGHT_PRICES_DOWN,
                with_model_name=name,
                with_level_block=level_block,
                with_transition=transition,
                model_cols=model_cols,
                **prices,
            )
            for prices in price_ladder
            for cols, name, level_block, transition, model_cols in head_ladder
        )
        tried: list[Text] = []
        for candidate in reserve:
            if fits(candidate, glyph_line):
                return candidate
            tried.append(candidate)
        # Не подошло ничего — и дальше важно, ПОЧЕМУ.
        affordable = [c for c in tried if width_of(c) <= available]
        if affordable:
            # Правая в свою ширину влезает; не хватило ЛЕВОЙ. Её беда снятием
            # имени профиля не лечится — ширину правой диктует строка цен, и
            # шапка на неё не влияет. Отдаём самый узкий из подошедших, а среди
            # равных по ширине — первый, то есть самый богатый.
            return min(affordable, key=width_of)
        # Правая не влезает даже в отведённую ей ширину: её обрежет терминал, и
        # действует прежний контракт вёрстки — самый бедный вариант, где имя
        # модели снято последним (точка остаётся).
        return tried[-1]

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
        лесенка `_compose_footer_right`."""
        return max(10, self._footer_total_width() - _FOOTER_CHROME_COLS)

    def _usage_segments(self) -> tuple[Text | None, Text | None]:
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
        settings = self.app.base_settings().settings
        return usage_segments(
            session_meta,
            request_meta,
            settings.pricing,
            settings.agent.model,
            settings.backend.completions_url,
        )

    def _tick_meta(self) -> dict:
        """Мета активной задачи — ТОЛЬКО для показа; читатели не мутируют.

        Вне тика опроса — свежее чтение (`{}` без стора или задачи). Внутри
        тика (`_poll_active`) — одно чтение на задачу, дальше тот же
        словарь: показ за тик видит один снимок. Любая запись `tui`-меты
        экрана идёт через `_write_tui_meta`, он сам сбрасывает снимок. Для
        read-modify-write снимок не годится — читать хранилище напрямую.
        `tui.pending_user_messages` через `_tick_meta` не читать.
        """
        if self._store is None or self._active_task_id is None:
            return {}
        if not self._in_poll_tick:
            return self._store.get_task_meta(self._active_task_id)
        tid = self._active_task_id
        if self._tick_meta_cache is None or self._tick_meta_cache[0] != tid:
            self._tick_meta_cache = (tid, self._store.get_task_meta(tid))
        return self._tick_meta_cache[1]

    def _write_tui_meta(self, task_id: str, patch: dict) -> None:
        """Записать `tui`-мету задачи; снимок тика после записи устарел."""
        self._store.update_tui_meta(task_id, patch)
        self._tick_meta_cache = None

    def _context_meter(self) -> Text | None:
        """Rich-Text индикатора контекста или None (скрыт)."""
        cfg = self.app.effective_settings()
        if str(cfg.settings.backend.api_mode) != "chat_completions":
            return None
        # Знаменатель шкалы — её mid-turn. turn-end (60%) подставляется только когда
        # mid-turn этой шкалы выключен: иначе проактивная уборка показывала бы 100%.
        if self._store is None or self._active_task_id is None:
            return None
        meta = self._tick_meta()
        # Знаменатель — mid-turn активной шкалы модели, которая ведёт историю.
        # mid-turn 0 подменяется turn-end той же шкалы.
        limits = self._meter_summary_limits(cfg)
        token_mid = limits.tokens_during
        token_end = limits.tokens_after
        saved_tokens = meta.get("last_main_prompt_tokens")
        if (
            not isinstance(saved_tokens, bool)
            and isinstance(saved_tokens, int)
            and saved_tokens > 0
            and (token_mid > 0 or token_end > 0)
        ):
            denominator = token_mid if token_mid > 0 else token_end
            if denominator <= 0:
                return None
            return render_context_meter(saved_tokens, denominator)

        threshold = limits.chars_during or limits.chars_after
        if threshold <= 0:
            return None
        # Источник символьной шкалы — meta.runtime.live_history_chars (persist).
        rt = meta.get("runtime") or {}
        if "live_history_chars" not in rt:
            return None
        try:
            chars = int(rt["live_history_chars"])
        except (TypeError, ValueError):
            return None
        return render_context_meter(chars, threshold)

    def _meter_summary_limits(self, cfg: dict):
        """Пороги для знаменателя индикатора контекста.

        Пока ход идёт — те, по которым сжимает РАБОТАЮЩИЙ агент
        (`runner.pinned_summary_limits`): его снимок конфига и уровень
        подготовленного вызова. /level действует со следующего нового вызова,
        /profiles требует пересборки runner, и по живым данным бар показывал бы не тот порог, по которому
        реально сработает сжатие. Вне хода — живой конфиг и живой Router: это
        пороги СЛЕДУЮЩЕГО хода."""
        runner = getattr(self.app, "active_runner", None)
        is_running = getattr(runner, "is_running", None)
        pinned = getattr(runner, "pinned_summary_limits", None)
        if callable(is_running) and is_running() and callable(pinned):
            limits = pinned()
            if limits is not None:
                return limits
        from gc_core.summarize.config import resolve_summary_limits_at_level

        router = None
        display_router = getattr(self.app, "display_router", None)
        if callable(display_router):
            try:
                router = display_router()
            except Exception:
                router = None
        return resolve_summary_limits_at_level(cfg, router, None)

    def _session_status_word(self) -> Optional[str]:
        """Сырое слово статуса сессии (без скобок): asking/running/completed/
        stopped/failed — или None, если исход НЕ записан (старая сессия / прогон
        не дошёл до чистого финиша). Единый источник для бейджа трейса (в скобках),
        заголовка окна (без скобок) и строки-итога. None → ничего не выдумываем:
        ни «stopped» в заголовке, ни строки-итога (см. _restore_latest_turn_line)."""
        # ask_human pending — агент ждёт ответа пользователя (приоритет).
        tui_state = getattr(self.app, "tui_state", None)
        if tui_state is not None and tui_state.ask_human_state is not None:
            return "asking"
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and runner.is_running():
            return "running"
        # Нет активного runner — статус из meta (tui-owned, затем legacy top-level).
        if self._store is not None and self._active_task_id is not None:
            meta = self._tick_meta()
            tui_status = (meta.get("tui") or {}).get("status")
            # Ход TUI держит процесс с замком сессии: «running» без него —
            # прерванный ход. Верхний статус пишут другие writer'ы — как есть.
            if tui_status == "running":
                return settled_status("running", live=self.open_run_is_live())
            if tui_status in ("completed", "stopped", "failed"):
                return tui_status
            if meta.get("status") == "running":
                return "running"
            if meta.get("status") == "completed":
                return "completed"
            if meta.get("status") in ("failed", "unknown_failed"):
                return "failed"
        return None  # исход неизвестен — не выдумываем «stopped»

    def open_run_is_live(self) -> bool:
        """Ход открытого треда жив: его держит живой процесс, или его исход
        этот экран ещё не записал (ход кончился, тик не дошёл)."""
        tid = self._active_task_id
        if tid is None:
            return False
        unsettled = self._was_running or self._run_pending
        runner = self._watched_runner or getattr(
            self.app, "active_runner", None
        )
        return (
            unsettled and self._run_task_id(runner) == tid
        ) or self.app.thread_run_is_live(tid)

    def _task_status_badge(self) -> Optional[str]:
        word = self._session_status_word()
        return f"[{word}]" if word else None

    def _refresh_header(self) -> None:
        left, right = self._compose_header_segments()
        try:
            update_if_changed(self.query_one("#header-left", Static), left)
            update_if_changed(self.query_one("#header-right", Static), right)
        except Exception:
            pass

    def _mode_pill(self, *, narrow: bool = False) -> Text:
        return render_mode_pill(
            self.app.effective_permission_mode(), narrow=narrow
        )

    def _footer_tail_commands(self) -> str:
        """Команды после пилюли режима: только то, без чего не обойтись.

        F-клавиш здесь нет намеренно (решение владельца): они перечислены в
        справке, а место в футере отдано режиму и правой колонке."""
        if self.app.tui_state.ask_human_state is not None:
            return "Esc/stop  Enter/ответить"
        return _FOOTER_RUNNING_LEFT if self._run_in_progress() else ""

    def _voice_busy(self) -> bool:
        """Идёт голосовая стадия (запись или распознавание)."""
        return self._voice is not None and self._voice.state != "idle"

    def _footer_row_width(self) -> int:
        """Контентная ширина строк левой части; до первого layout — 0."""
        try:
            return self.query_one(_SEL_FOOTER_MODE, Static).content_size.width
        except Exception:
            return 0

    def _footer_mode_variants(self) -> tuple[Text, Text]:
        """Строка 1 в двух формах пилюли режима: полной и глифом (+ хвост).

        Во время записи обе формы — пилюля голоса. Ими же меряет строку 1
        лесенка правой колонки: отдельный оценщик разошёлся бы с отрисовкой."""
        if self._voice_busy():
            voice = self._voice_footer_render()
            return voice, voice
        tail = self._footer_tail_commands()
        full, glyph = self._mode_pill(), self._mode_pill(narrow=True)
        if tail:
            full.append(f"{PILL_GAP}{tail}")
            glyph.append(f"{PILL_GAP}{tail}")
        return full, glyph

    def _footer_mode_line(self) -> Text:
        """Строка 1: пилюля режима + хвост команд; во время записи — пилюля
        голоса.

        Пилюля — в самой богатой форме, что встаёт в строку целиком: полная,
        затем один глиф. Не встаёт и глиф — уступает он: хвост (команда
        остановки, ответ агенту) не теряется ни на какой ширине. Ширина 0
        (layout ещё не прошёл) — полная форма; resize пересоберёт по месту."""
        variants = self._footer_mode_variants()
        if self._voice_busy():
            return variants[0]
        width = self._footer_row_width()
        for line in variants:
            if width <= 0 or line.cell_len <= width:
                return line
        tail = self._footer_tail_commands()
        return Text(tail) if tail else variants[1]

    def _footer_slot_line(self, badge: str) -> Text:
        """Строка 2 (слот): подсказка/отказ/совет, иначе бейдж уведомлений."""
        if self._footer_hint_active and self._footer_mode_hint is None:
            return self._footer_slot_with(self._footer_hint_body())
        return self._footer_badge_line(badge)

    def _footer_hint_body(self) -> Text:
        """Текст подсказки слота; у отклика команды — цвет: отказ целиком
        WARN, успех — глиф ✓ цветом OK."""
        body = Text(self._footer_hint_text or "")
        if self._footer_hint_tone == "warn":
            body.stylize(_WARN)
        elif self._footer_hint_tone == "ok":
            body.stylize(_OK, 0, 1)
        return body

    def _footer_badge_line(self, badge: str) -> Text:
        """Слот с бейджем уведомлений — его ширину резервирует лесенка."""
        return self._footer_slot_with(Text(badge))

    def _footer_slot_with(self, body: Text) -> Text:
        """Слот с телом `body`. Во время записи первым стоит глиф режима его
        цветом: строку 1 заняла пилюля голоса, а режим должен быть виден."""
        out = Text()
        if self._voice_busy():
            out.append_text(self._mode_pill(narrow=True))
        if out and body:
            out.append(PILL_GAP)
        out.append_text(body)
        return out

    def _footer_left_rows(self, badge: str) -> tuple[Text, Text]:
        """Обе строки левой части по текущему состоянию.

        Пояснение режима — исключение: «пилюля — пояснение» переносится на
        обе строки, хвост и бейдж на это время скрыты. Во время записи оно
        не рисуется — строка 1 отдана пилюле голоса."""
        if self._footer_mode_hint is not None and not self._voice_busy():
            return mode_hint_rows(
                self._footer_mode_hint, self._footer_row_width()
            )
        return self._footer_mode_line(), self._footer_slot_line(badge)

    def _refresh_notices_footer(self) -> None:
        """Событие уведомлений: та же сборка — бейдж влияет и на правую
        колонку (её лесенка резервирует его ширину)."""
        self._refresh_footer()

    def _footer_frame(self) -> _FooterFrame:
        """Данные кадра футера: каждое резолвится один раз на обе колонки.

        Части независимы: сбой одной (Router, мета треда, уведомления)
        гасит только её, остальной футер живёт. Снимок шапки None — правая
        колонка на этот кадр не собирается."""
        part = self._footer_part
        return _FooterFrame(
            badge=part(
                "badge",
                lambda: format_notices_badge(self._collect_footer_notices()),
                "",
            ),
            snapshot=part("snapshot", self._footer_head_snapshot, None),
            meter=part("meter", self._context_meter, None),
            usage=part("usage", self._usage_segments, (None, None)),
        )

    def _footer_part(self, kind: str, resolve, fallback):
        """`resolve()` или `fallback` при сбое; сбой пишется в лог один раз
        на эпизод — до первого успешного резолва этой части."""
        try:
            value = resolve()
        except Exception:
            if kind not in self._footer_faults:
                self._footer_faults.add(kind)
                _log.exception("футер: сбой части %s", kind)
            return fallback
        self._footer_faults.discard(kind)
        return value

    def _footer_inputs(self, frame: _FooterFrame) -> tuple:
        """Ключ сборки футера: всё, от чего зависит его вид.

        Неизменяемый кортеж примитивов; Rich `Text` — по `text_key` (текст
        и стили), не по identity. Вход, не попавший сюда, футер не увидит
        до смены другого входа."""
        voice = self._voice_footer_render() if self._voice_busy() else None
        return (
            self._footer_total_width(),
            self._footer_row_width(),
            self.app.effective_permission_mode(),
            self._run_in_progress(),
            self.app.tui_state.ask_human_state is not None,
            frame.badge,
            frame.snapshot,
            text_key(frame.meter),
            tuple(text_key(seg) for seg in frame.usage),
            text_key(voice),
            self._footer_hint_active,
            self._footer_hint_text,
            self._footer_hint_tone,
            self._footer_mode_hint,
        )

    def _refresh_footer(self) -> None:
        """Пересобрать футер, если изменились его входные данные.

        Тик опроса зовёт его каждые 250 мс, поэтому без изменений он дёшев:
        данные кадра резолвятся, лесенка правой колонки и строки левой — нет.
        Все пути перерисовки (тик, подсказки, режим, голос, уведомления,
        resize) идут сюда: изменение одного входа пересобирает обе колонки.
        """
        try:
            right = self.query_one("#footer-right", Static)
            mode = self.query_one(_SEL_FOOTER_MODE, Static)
            slot = self.query_one(_SEL_FOOTER_SLOT, Static)
        except Exception:
            return
        # Подсказка уступает место запущенному прогону: пока отказ дожёвывает
        # свои 5 секунд, человек мог отправить запрос — футер обязан показать
        # «Ctrl+C/stop», а не устаревший текст (живой смок владельца 2026-09-04).
        if (
            self._footer_refusal_active
            and self._run_in_progress()
            and self.app.tui_state.active_task_id != self._refusal_run_id
        ):
            self._reset_footer_hint_state()
        frame = self._footer_frame()
        key = self._footer_inputs(frame)
        if key == self._footer_key:
            return
        right_built = self._build_footer_right(right, frame)
        first, second = self._footer_left_rows(frame.badge)
        update_if_changed(mode, first)
        update_if_changed(slot, second)
        # Ключ — только за собранный футер: сбой сборки правой не входит в
        # ключ, и следующий тик обязан попробовать снова.
        self._footer_key = key if right_built else None

    def _build_footer_right(self, widget: Static, frame: _FooterFrame) -> bool:
        """Собрать правую колонку; False — сборка упала (левая живёт).

        Без снимка шапки колонка держит прежний вид: снимок в ключе, его
        возврат пересоберёт футер."""
        if frame.snapshot is None:
            return True
        content = self._footer_part(
            "right", lambda: self._compose_footer_right(frame), None
        )
        if content is None:
            return False
        update_if_changed(widget, content)
        return True

    def on_screen_resume(self) -> None:
        """После возврата с под-экрана — освежить карточку и футер уведомлений."""
        self._refresh_notices_card()
        self._refresh_footer()

    def _current_cwd(self) -> str:
        """Рабочая папка сессии — фиксирована = папка запуска процесса."""
        return str(Path.cwd())

    def _show_footer_refusal(
        self, text: str, tone: Optional[str] = "warn"
    ) -> None:
        """Содержательный отказ («✗ не сохранено: <причина>») в слоте футера;
        цвет отказа — WARN, у всех отказов слота (`tone=None` — совет).

        Отдельно от `_show_footer_hint`: у отказа есть причина с координатой
        («slots.default: алиас "ghost" не найден…»), и 2-секундного дефолта
        эфемерных подсказок не хватает её прочитать (живой смок владельца
        2026-09-04) — отказ держится 5с, как paste-hint. Но не ценой
        актуальности: следующее действие человека (клавиша, печать, запуск
        прогона) гасит его раньше — `_footer_refusal_active` сужает это правило
        до отказов (и советов картинок, `_show_footer_tip`), esc-confirm и paste-hint
        живут по своим канонам."""
        self._show_footer_hint(text, seconds=5.0, tone=tone)
        self._footer_refusal_active = True
        # Снимок прогона НА МОМЕНТ постановки: «прогон вытесняет отказ» — только
        # про прогон, начавшийся ПОСЛЕ. Отказ, поставленный во время работы
        # агента (/gate из палитры), без снимка гас первым же тиком опроса —
        # 5 секунд не читались никогда именно там, где отказы и жмут (ревью
        # 2026-09-04, находка 1).
        self._refusal_run_id = (
            self.app.tui_state.active_task_id
            if self._run_in_progress()
            else None
        )
        # Снимок поля ввода — то же правило для печати: отказ гасит правка ПОСЛЕ него.
        # Changed от правки, сделанной до отказа (запасная вставка при полном лимите
        # картинок), доставляется позже и текст не меняет — отказ живёт.
        try:
            self._refusal_input_text = self.query_one(
                _SEL_CMDINPUT, CommandTextArea
            ).text
        except Exception:
            self._refusal_input_text = ""

    def _show_footer_outcome(self, text: str, *, ok: bool) -> None:
        """Итог команды в слоте футера по канону отказа (5 с, гасится
        следующим действием), с цветом: отказ — WARN, успех — ✓ цветом OK."""
        self._show_footer_refusal(text, tone="ok" if ok else "warn")

    def _show_footer_tip(self, text: str) -> None:
        """Совет в слоте футера (подсказки картинок) — по канону отказа: 5 с,
        следующее действие человека гасит его раньше."""
        self._show_footer_refusal(text, tone=None)

    def _footer_tip_fits(self) -> bool:
        """Совету есть где прозвучать: перед человеком поле ввода, оно принимает ввод, и
        футер не занят другой подсказкой, отказом или голосом."""
        if self.app.screen is not self or self._footer_hint_active:
            return False
        if self._voice is not None and self._voice.state != "idle":
            return False
        try:
            return not self.query_one(_SEL_CMDINPUT, CommandTextArea).disabled
        except Exception:
            return False

    def _show_footer_hint(
        self, text: str, seconds: float = 2.0, tone: Optional[str] = None
    ) -> None:
        """Короткая подсказка в слоте футера (esc-confirm — 2с, paste-hint —
        5с, содержательные отказы — `_show_footer_refusal`)."""
        # Новая подсказка снимает режим отказа: иначе paste-hint, показанный
        # ПОСЛЕ отказа, гасился бы первой печатью — не по своему канону
        # (`_show_footer_refusal` ставит флаг заново, после этого вызова).
        self._footer_refusal_active = False
        self._footer_hint_tone = tone
        self._footer_hint_active = True
        self._footer_hint_text = text
        self._footer_mode_hint = None
        self._refresh_footer()
        if self._footer_hint_timer is not None:
            self._footer_hint_timer.stop()
        self._footer_hint_timer = self.set_timer(
            seconds, self._clear_footer_hint
        )

    def on_permission_mode_changed(self, mode: str) -> None:
        """Смена режима: пилюля перерисовывается сразу, плюс короткое пояснение.

        Пояснение склеивается с пилюлей в одну фразу («⏸ manual mode —
        подтверждение всех действий, кроме чтения») и занимает обе строки
        левой части на 2,5 с: имя стоит в пилюле, текст его не повторяет.
        Во время записи режим меняется сразу (глиф в слоте), а пояснение
        ждёт её конца — строка 1 занята пилюлей голоса.
        """
        self._refresh_footer()
        if mode not in MODE_HINTS:
            return
        if self._voice_busy():
            self._mode_hint_after_voice = mode
            return
        self._show_footer_hint(MODE_HINTS[mode], seconds=2.5)
        self._footer_mode_hint = mode
        self._refresh_footer()

    def _show_esc_hint(self, kind: str) -> None:
        """Esc-confirm hint: запоминает kind (для 2-го Esc) + показывает подсказку."""
        self._esc_confirm_kind = kind
        self._show_footer_hint(HINT_TEXT[kind])

    def _reset_footer_hint_state(self) -> None:
        """Снять hint-state без перерисовки — для вызова ИЗ `_refresh_footer`
        (полный `_clear_footer_hint` зовёт его сам и зациклился бы)."""
        self._esc_confirm_kind = None
        self._footer_hint_active = False
        self._footer_hint_text = None
        self._footer_hint_tone = None
        self._footer_mode_hint = None
        self._footer_refusal_active = False
        self._refusal_run_id = None
        if self._footer_hint_timer is not None:
            self._footer_hint_timer.stop()
            self._footer_hint_timer = None

    def _clear_footer_hint(self) -> None:
        """Сбросить hint-state (esc + paste) и вернуть слоту бейдж.

        Пока идёт вставка изображения, покой футера — «Вставка…» (контракт картинок §2):
        отказ, «дождитесь…» и Esc-подсказка гаснут обратно в неё, а не в команды."""
        self._reset_footer_hint_state()
        if self._image_paste_busy:
            self._show_footer_hint(
                IMAGE_PASTE_BUSY_HINT, seconds=_IMAGE_PASTE_HINT_SECONDS
            )
            return
        self._refresh_footer()

    def _run_in_progress(self) -> bool:
        """Прогон реально идёт: активная задача + живой раннер. Один предикат на
        выбор `Ctrl+C/stop` в строке режима и на уступку подсказки прогону."""
        runner = getattr(self.app, "active_runner", None)
        return (
            self.app.tui_state.active_task_id is not None
            and runner is not None
            and runner.is_running()
        )

    # --- голосовой ввод (Ctrl+T) ---------------------------------------

    def _voice_config(self) -> dict:
        """voice-секция + прокси агента: у голоса своего поля прокси нет, а в облако
        он ходит по тем же правилам, что backend (копия — оригинал не трогаем).

        Читаем из ЕДИНОЙ точки (`effective_settings`), как витрины и сборка executor:
        база окна не знает про сессионный слой, и профиль, выбранный на
        сессию, со своей секцией `voice` до диктовки бы не доехал."""
        from gc_core.config_modules.field_meta import plain_dump

        settings = self.app.effective_settings().settings
        # Сырой токен: транскрайбер шлёт его в заголовке Authorization.
        voice = plain_dump(settings.voice)
        voice.setdefault("proxy", settings.backend.proxy)
        return voice

    def _ensure_voice_flow(self):
        """Ленивая сборка flow (импорты voice-модулей — только по факту)."""
        if self._voice is not None:
            return self._voice
        from gc_tui.services.voice_input import VoiceInputFlow
        from gc_tui.voice.recorder import MicRecorder
        from gc_tui.voice.transcriber import transcribe

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
        from gc_core.config_modules.voice import voice_enabled

        try:
            inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
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
        from gc_tui.voice.recorder import available

        if not available():
            self.notify(
                "голосовой ввод: аудио-стек sounddevice недоступен (Linux: установите libportaudio2)",
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
            from gc_tui.services.voice_setup import status_cheap

            # единая точка, как в _voice_config
            return status_cheap(self.app.effective_settings()).mode == "local"
        except Exception:
            return False

    def _start_recording_after_service_check(self) -> None:
        """Померить порт ФОНОМ и по результату начать запись либо поднять сервис.

        Проверка дешёвая (TCP-connect на loopback), но на мёртвом порту сгорает полным
        таймаутом — в UI-потоке это заметный фриз, поэтому воркер (канон живости
        `/settings`). Подъём тем более: холодный старт — до полутора минут."""
        if self._voice_service_pending:
            return  # проверка/подъём уже идут — второй не заводим
        self._voice_service_pending = True

        def _work() -> None:
            from gc_tui.services.voice_setup import probe_alive

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
        self.notify(
            "голосовой сервис не отвечает — поднимаю…", severity="warning"
        )
        self._raise_voice_service()

    def _raise_voice_service(self) -> None:
        """Поднять батарейку фоном. Запись не начинаем сами: холодный старт долгий, и
        человек за это время успевает уйти — зовём его обратно тостом."""
        self._voice_service_pending = True
        cfg = (
            self.app.effective_settings()
        )  # единая точка, как в _voice_config

        def _work() -> None:
            from gc_tui.voice_runtime.autostart import ensure_voice_service

            try:
                ok = ensure_voice_service(cfg)
            except Exception:
                ok = False
            self.app.call_from_thread(self._on_voice_service_raised, ok)

        self.run_worker(_work, thread=True, name="voice-service-raise")

    def _on_voice_service_raised(self, ok: bool) -> None:
        self._voice_service_pending = False
        if ok:
            self.notify(
                "голосовой сервис поднят — можно диктовать",
                severity="information",
            )
        else:
            self.notify(
                "не удалось поднять голосовой сервис — `gc-voice status` покажет, что с ним",
                severity="warning",
            )

    def _voice_footer_render(self):
        """Пилюля + dim-хвост клавиш (хвост — только на записи)."""
        # Кадр анимации — по стадии. Записи частый тик нужен эквалайзеру, но мигание
        # «● REC» отмерено в кадрах и на ускоренном тике задрожало бы: ему отдаём
        # каждый второй. Волне распознавания частые кадры, наоборот, в плюс — она
        # летит дробными позициями, и чем мельче шаг, тем ровнее полёт.
        frame = (
            self._voice_frame
            if self._voice.state == "transcribing"
            else self._voice_frame // 2
        )
        pill = render_pill(
            self._voice.state,
            self._voice.elapsed_seconds,
            frame,
            bands=self._voice.input_bands,
        )
        if self._voice.state == "recording":
            pill.append("  Ctrl+T / Enter — стоп · Esc — отмена", style=_DIM)
        return pill

    def _on_voice_state(self, state: str) -> None:
        try:
            row = self.query_one(_SEL_INPUT_ROW, Horizontal)
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
            pending, self._mode_hint_after_voice = (
                self._mode_hint_after_voice,
                None,
            )
            if pending is not None:
                self.on_permission_mode_changed(pending)
                return
        elif self._voice_timer is None:
            self._voice_frame = 0
            # 0.08 с (было 0.15): эквалайзер обязан успевать за голосом, а звук теперь
            # приходит порциями по 32 мс. Расчёт кадра дешёвый и вне звукового потока.
            self._voice_timer = self.set_interval(0.08, self._voice_tick)
        if state == "transcribing":
            self._voice_frame = 0  # эмиттер rec-режима стартует с пустой ленты
        self._refresh_footer()

    def _voice_tick(self) -> None:
        if self._voice is None or self._voice.state == "idle":
            return
        self._voice_frame += 1
        self._voice.tick()  # авто-стоп max_seconds
        self._refresh_footer()

    def _on_voice_error(self, message: str) -> None:
        self.notify(message, severity="warning")

    def _insert_transcribed(self, text: str) -> None:
        """Вставить распознанный текст в позицию курсора (+ авто-пробел)."""
        try:
            inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
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
        self.query_one(_SEL_CMDINPUT, CommandTextArea).focus()
        self._show_brand()
        self._render_startup_notices()
        # Per-project история input-реплик: project_dir известен со старта
        # (threads_dir.parent), attach не нужен — ↑/↓ работают уже в empty-state.
        from gc_tui.services.input_history import InputHistory

        threads_dir = Path(self.app._threads_dir or "")
        project_dir = threads_dir.parent if threads_dir.parts else Path.cwd()
        self._history = InputHistory(project_dir)
        self._history.load()
        self.set_interval(
            0.25, self._poll_active
        )  # 250 ms — single source of truth
        # Прогрев executor'а (incl. MCP-дискавери) в фоне на старте — чтобы MCP
        # грузились при запуске gc-ua, а не на первом сообщении. Индикатор —
        # из _poll_active. Build-once: первый start() переиспользует собранный.
        # warm — опциональная оптимизация: прод-runner (AgentRunner) её имеет,
        # тест-дабли раннера могут не иметь — тогда просто пропускаем.
        runner = self.app.get_runner()
        warm = getattr(runner, "warm", None)
        if callable(warm):
            warm()
        self._warm_skills_cache()
        self._sync_indicator_bar()  # пустой старт: бара нет
        # Совет вставить картинку из буфера: на старте и при возвращении в окно
        # терминала — буфер меняется только снаружи; переходы внутри приложения его не
        # проверяют. Без сообщений терминала о фокусе окна — только на старте.
        self.watch(self.app, "app_focus", self._on_terminal_focus, init=False)
        self.watch(
            self.query_one(_SEL_TRACE, VerticalScroll),
            "scroll_y",
            self._on_trace_scrolled,
            init=False,
        )
        self.watch(self, "focused", self._on_focus_moved_overlays, init=False)
        self._check_clipboard_tip()

    def _reset_render_state(self) -> None:
        """Clear all per-task rendering state. Called on task switch and stream_reset."""
        self._task_events.clear()
        self._last_turn_outcome = None
        self._outcome_superseded = False
        self._rendered_ids.clear()
        self._segment_starts.clear()
        self._answer_flow.new_turn()
        self._reset_new_replies()
        self._next_no = 1
        self._stick_bottom = (
            True  # новый/переключённый трейс → показываем низ (latest)
        )
        self._last_auto_y = 0
        self._active_calls.clear()
        self._call_timers.clear()
        self._block_views.clear()
        self._subagent_progress.reset()
        self._nested_tool_rows.clear()
        self._nested_widgets_by_parent.clear()
        self._subagent_parent_rows.clear()
        self._submit_solution_call_ids.clear()
        self._submit_solution_widgets.clear()
        self._response_tail = []
        self._summary_body_widgets.clear()
        self._summary_marker_widgets.clear()
        self._held_reveals.clear()
        self._pending_user_widgets.clear()  # spec 2026-05-26
        self._held_client_ids.clear()
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
        self._ask_human_question_row = (
            None  # виджет ушёл вместе с trace-очисткой
        )
        self._ask_human_owner_runner = None
        self._ask_human_child_parent_call_id = None
        # I2: cancel_confirm всегда (даже если баннер не монтировался).
        _reset_runner = getattr(self.app, "active_runner", None)
        if _reset_runner is not None and hasattr(
            _reset_runner, "cancel_confirm"
        ):
            try:
                _reset_runner.cancel_confirm()
            except Exception:
                pass
        if self._confirm_active:
            try:
                self._hide_confirm_banner()
            except Exception:
                pass
        if self._plan_banner is not None:
            try:
                self._hide_plan_banner()
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
        # На смене задачи / stream_reset история событий обнуляется,
        # индикатор должен начать с чистого состояния.
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
            threads_dir,
            self.app._system,
            tid,
            tui_id,
            last_mono=self._session_lock_hb_mono,
        )

    def attach_active_task(
        self, task_id: str, *, is_resume: bool = False
    ) -> bool:
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
        if prev != task_id:
            self._copy_seq += 1  # итог /copy прошлой сессии сюда не пишется
            self._leave_thread_view()
        self._active_task_id = task_id
        self.app.tui_state.active_task_id = task_id
        self._session_lock_hb_mono = 0.0
        self._live_turn_tool_calls = (
            0  # смена задачи (не stream_reset: там счётчик хода нужен)
        )
        # Сброс снимает и индикатор: его работа — прежнего треда.
        self._reset_render_state()
        self._store = FileTaskStore(
            threads_dir=self.app._threads_dir or "",
            system=self.app._system,
        )
        # Настройки режимов принадлежат треду (спека 2026-07-20-session-scoped-settings):
        # берём его значения; их нет (старый тред) — сессия возвращается к умолчанию.
        task_meta = self._store.get_task_meta(task_id)
        saved = read_from_meta(task_meta)
        self._adopt_thread_breaker(task_meta)
        # Пересобираем executor только при РЕАЛЬНОЙ смене набора: attach идёт и на
        # каждом автосозданном треде, и на возврате к тому же — прогретый раннер
        # (с уже пройденным MCP-дискавери) выбрасывать зря нельзя.
        if self.app.load_session_overrides(saved or {}):
            self.app.invalidate_runner()
        # Сессионный выбор профиля, ставший битым под текущей базой (§9.4) — снять
        # + notice + пересборка; санация ПОСЛЕ load_session_overrides — проверяет то,
        # что реально стало активным оверрайдом этого треда.
        self.app.sanitize_session_profile()
        # Режим разрешений — тоже выбор, сохранённый в сессии; у открытой
        # пикером сессии без режима — режим конфига. Флаг запуска сильнее.
        self.app.adopt_thread_permission_mode(task_meta, resumed=is_resume)
        # read_all_events — атомарный снимок live-файла + cursor (как раньше).
        # Была ли компакция, определяем по голове live-снимка (после turn_end
        # первая запись = сводка) — без доп. I/O. Тред без компакции рендерим
        # старым путём из ЭТОГО снимка (consistent с cursor, без лишних чтений —
        # иначе перетасовка тайминга ломает гонку optimistic-reply). read_segments
        # (glob архивов + повторное чтение) — только для сжатых тредов.
        records, cursor = self._store.read_all_events(task_id)
        compacted = bool(records) and self._store.is_compaction_summary(
            records[0]
        )
        self._cursor = cursor
        # Очистить #trace от предыдущей сессии — иначе при переходе
        # session A → session B (через /sessions) трейсы склеиваются
        # (виджеты A остаются, B дорисовывается поверх).
        self._clear_trace()
        if compacted:
            self._render_segments(self._store.read_segments(task_id))
        else:
            self._segment_starts = [0]
            self._render_records(records)
        self._merge_sidecar_nested(task_id)
        self._refresh_header()
        self._refresh_footer()
        sp = self._session_last_prompt()
        if is_resume:
            self._resumed_prompt = sp  # авторитетно из стора (переключение)
            self._restore_latest_turn_line()  # строка итога прошлого хода
            self._sync_plan_panel_with_session()
        elif sp is not None:
            self._resumed_prompt = sp  # auto-create: не затирать свежий send
        self._update_terminal_title()  # статус + последний промпт сессии в таб
        self._sync_indicator_bar()  # бар — по прогону открытого треда
        self._mount_failed_from_meta(task_id)
        # Смена сессии: плашки буфера ввода — в последовательность этого треда.
        # Автосоздание треда под отправку (prev == task_id) номера не трогает:
        # только что отправленные картинки уже несут номера этого треда.
        if prev != task_id:
            self._renumber_input_images()
            # Намерение «обсудить план» принадлежит прежней сессии.
            self._plan_rework_pending = False
        self._sync_input_placeholder()

        # Свести ask_human-баннер с реальностью: replay/компакция могли оставить
        # баннер для уже отвеченного вопроса (его output суммаризован) — см. helper.
        # Висячий вопрос (реально последний, без живого агента) → remnant с меткой.
        self._reconcile_ask_human_after_render(allow_remnant=True)
        return True

    def on_unmount(self) -> None:
        """App exit / снятие экрана — отпустить lock удерживаемого tid и закрыть сеанс
        поиска файлов (его поток не должен висеть без экрана)."""
        self._stamp_quit_outcome()
        self._session_lock_release()
        if self._file_search is not None:
            self._file_search.close()

    def _stamp_quit_outcome(self) -> None:
        """Выход посреди хода: ход умирает с процессом, исход — в его тред.

        Только мета: экран уже снимается, ленту не трогаем. Ход ещё бежит —
        «stopped»; успел кончиться до тика — его собственный исход."""
        if not (self._was_running or self._run_pending) or not self._store:
            return
        runner = self._watched_runner or getattr(
            self.app, "active_runner", None
        )
        if getattr(runner, "_thread_kind", "run") != "run":
            return
        tid = self._run_task_id(runner)
        if tid is None:
            return
        if runner is not None and runner.is_running():
            self.app.last_stop_requested = True
        self._stamp_run_outcome(tid, self._runner_outcome(runner)[0], None)

    def _leave_thread_view(self) -> None:
        """Смена открытого треда: то, что экран держал о ходе прежнего,
        новому не принадлежит. Остановленный ход дорабатывает отвязанным —
        его мигающий «■» остаётся в прежней сессии. Откат по Esc — последней
        отправки этого вида: в открытой сессии её не было, откатывать нечего
        до новой отправки."""
        self._stopping_runner = None
        self._last_user_message_id = None
        self._closed_turn_summary = None

    def _mount_failed_from_meta(self, task_id: str) -> None:
        """После рестарта: смонтировать виджеты failed-реплик из
        meta.tui.pending_user_messages — но только truly-orphaned (не нашли
        соответствующего user-сообщения ни в meta.user_intent_log,
        ни в self._task_events). Реплика очереди при живом прогоне
        (пересборка ленты посреди хода) — снова в очереди, не failed.

        Source of truth — meta.user_intent_log (persisted across
        summarizations; user_ask-записи); legacy meta.user_messages_log и
        _task_events — safety net на случай пустого/старого log."""
        if self._store is None:
            return
        from gc_tui.services.relative_time import parse_iso_utc

        meta = self._store.get_task_meta(task_id)
        records = (meta.get("tui") or {}).get("pending_user_messages", [])
        if not records:
            return
        alive = self._run_in_progress()
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
            if t is not None and (
                latest_delivered_ts is None or t > latest_delivered_ts
            ):
                latest_delivered_ts = t
        truly_orphaned: list[dict] = []
        kept_records: list[dict] = []
        for r in records:
            wire = r.get("wire_content") or ""
            content = r.get("content") or ""
            # Считаем доставленным, если совпал либо clean content (новый формат,
            # user_messages_log хранит display_content), либо wire (старый формат,
            # где wire == content, или intent_log хранит wire напрямую).
            if (content and content in delivered_wires) or (
                wire and wire in delivered_wires
            ):
                continue
            # Устарело: есть доставленный user_ask с ts позже этой реплики →
            # пользователь ушёл дальше; не монтируем и вычищаем из meta.
            if latest_delivered_ts is not None:
                r_ts = parse_iso_utc(r.get("ts_utc_iso"))
                if r_ts is not None and r_ts < latest_delivered_ts:
                    continue
            truly_orphaned.append(r)
            waits = alive and bool(r.get("message_id"))
            kept_records.append(
                {**r, "status": "failed"}
                if r.get("status") == "pending" and not waits
                else r
            )
        for r in truly_orphaned:
            self._mount_orphaned_reply(r, alive=alive)
        self._refresh_queue_hint()
        if kept_records != records:
            self._write_tui_meta(
                task_id, {"pending_user_messages": kept_records}
            )

    def _mount_orphaned_reply(self, record: dict, *, alive: bool) -> None:
        """Реплика из meta, которой нет в треде. Отправленная в очередь
        (у записи есть `message_id`) при живом прогоне ещё ждёт агента —
        снова в очереди, её сверит тред. Иначе — `failed`."""
        content = record.get("content") or ""
        message_id = record.get("message_id") if alive else None
        if not (content or message_id):
            return
        images = attachments_of(record)
        if not message_id:
            self._mount_user_reply(content, state="failed", images=images)
            return
        client_id = record.get("client_id")
        if client_id:
            self._pending_wire_cache[client_id] = (
                record.get("wire_content") or ""
            )
        self._mount_user_reply(
            content,
            state="queued",
            client_id=client_id,
            images=images,
            skill=record.get("skill"),
            message_id=message_id,
        )

    def _render_segments(self, segments: list[dict]) -> None:
        """Рендер ВСЕХ сегментов сырьём — полный трейс, без схлопывания старых
        сегментов в маркер. Маркер сжатия сегмента i = его compacted_by (ставится
        между сегментами, чтобы было видно, где прошла компакция).

        Дедуп по id — в `_render_record` (общий для всех путей): preserved-копия
        лежит в следующем файле с тем же id, рисуется первое вхождение.
        Payload остаётся full, пока этот id есть в окне segments[-2:]."""
        records = [rec for seg in segments for rec in seg["messages"]]
        context = _records_context(records, self._current_cwd())
        # Ответ модели, начатый в конце ленты, нужен вызову следующего тика.
        self._response_tail = response_tail(
            [rec.get("payload") for rec in records if isinstance(rec, dict)]
        )
        window_start = retention_window_start_index(len(segments))
        window_ids = message_ids_in_retention_window(segments)
        for i, seg in enumerate(segments):
            seg_in_window = i >= window_start
            self._segment_starts.append(len(self._task_events))
            for rec in seg["messages"]:
                payload = rec.get("payload")
                mid = payload.get("id") if isinstance(payload, dict) else None
                in_window = seg_in_window or (
                    mid is not None and mid in window_ids
                )
                self._render_record(rec, context=context, in_window=in_window)
            comp = seg["compacted_by"]
            if comp is not None:
                self._append_and_render(
                    build_compaction_marker_event(self._next_no, i, comp)
                )

    def _append_and_render(self, ev, *, live: bool = False) -> None:
        """Событие с номером _next_no — в _task_events и на экран."""
        self._next_no += 1
        self._task_events.append(ev)
        self._render_event(ev, live=live)

    def _render_records(
        self,
        records: list[dict],
        *,
        live: bool = False,
        context: RenderContext | None = None,
    ) -> None:
        payloads = [
            rec.get("payload") for rec in records if isinstance(rec, dict)
        ]
        if live:
            # Ответ модели мог начаться в прошлой пачке опроса: его текст
            # нужен вызову turn_finish, пришедшему сейчас.
            payloads = [*self._response_tail, *payloads]
        self._response_tail = response_tail(payloads)
        if context is None:
            context = collect_render_context(
                payloads, root=self._current_cwd()
            )
        elif live:
            # Общий контекст шага сжатия не видит текста ответа из прошлой
            # пачки опроса.
            context = with_closing_replies(context, payloads)
        for rec in records:
            self._render_record(rec, live=live, context=context)

    def _render_finish(self, ev) -> None:
        """Конец хода turn_finish: отчёт ответом, запись «итог» или строка
        «заметка» (правило — turn_close.finish_close)."""
        from gc_tui.services.trace_render import (
            build_finish_widgets,
            finish_close_of,
        )

        # A1: отчёт, уже показанный прозой, вторым блоком не рисуем. База
        # дедупа — только проза: попытки хода убирает свёртка ниже.
        close = self._answer_flow.finish(
            finish_close_of(ev), tracked=bool(ev.call_id)
        )
        following = self._stick_bottom
        widgets = build_finish_widgets(close, event_no=ev.no)
        for w in widgets:
            self._trace_mount(w, scroll=False)
        counted = bool(close.body.strip() or close.note) and (
            self._count_new_reply(following)
        )
        if following:
            self._scroll_trace_end()
        if not ev.call_id:
            return
        self._submit_solution_call_ids.add(ev.call_id)
        self._drop_finish_attempts()
        self._submit_solution_widgets.append(widgets)
        self._new_attempt_replies = int(counted)
        # Финальный ответ виден — переключаем индикатор в режим
        # «✓ завершаю…» немедленно (без 250мс лага на следующий тик
        # _poll_active). Sticky-маркер sentinel выставляется в
        # _render_records, чтобы пережить _last_event_kind = ev.kind.
        if self._thinking is not None:
            self._thinking.set_state("validating")

    def _drop_finish_attempts(self) -> None:
        """Убрать прошлые попытки turn_finish хода.

        Отклонённые intent_guard'ом попытки убираем молча (как regenerate
        в чатах): внутренняя кухня качества юзеру не показывается, обе
        попытки сохранены в jsonl треда. Убранная попытка уходит и из
        счётчика плашки: он равен видимым новым ответам."""
        for prev_widgets in self._submit_solution_widgets:
            for w in prev_widgets:
                w.remove()
        self._submit_solution_widgets.clear()
        if self._new_attempt_replies:
            self._new_replies -= self._new_attempt_replies
            self._new_attempt_replies = 0
            self._refresh_trace_overlays()

    def _render_record(
        self,
        rec: dict,
        *,
        live: bool = False,
        context: RenderContext | None = None,
        in_window: bool = True,
    ) -> None:
        if is_turn_outcome_record(rec):
            # Итог хода — не сообщение ленты: строка последнего хода, пока
            # после записи не началась реплика.
            outcome = parse_turn_outcome(rec)
            if outcome is None or self._is_undone_reply(outcome.opened_by):
                return
            self._last_turn_outcome = outcome
            self._outcome_superseded = False
            self._end_turn_blocks(outcome)
            self._mount_turn_line(outcome)
            return
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            return
        # Реплика после записи — начало следующего хода (и та, что экран
        # уже показал сам: важна позиция в файле, а не повтор).
        if extract_real_user_message_text(payload):
            self._outcome_superseded = True
            self._turn_started_after_record(live=live)
        mid = payload.get("id")
        if mid is not None:
            if mid in self._rendered_ids:
                return
            self._rendered_ids.add(mid)
        ev = map_message(
            payload,
            no=self._next_no,
            ts=rec.get("ts", ""),
            context=context,
        )
        if ev is None:
            return
        apply_payload_retention(ev, seg_in_window=in_window)
        self._append_and_render(ev, live=live)
        # Spec 2026-05-26 + 2026-05-28: запоминаем kind последнего события.
        # Для submit_solution финального ответа — sticky sentinel
        # "_finalized" вместо "agent": блокирует повторный mount в
        # _poll_active, пока не придёт новое значимое событие (gate-reject
        # [policy], новый submit_solution и т.п.).
        if ev.kind == "agent" and ev.tool_name == "submit_solution":
            self._last_event_kind = "_finalized"
        else:
            self._last_event_kind = ev.kind

    def _is_undone_reply(self, msg_id: Optional[str]) -> bool:
        """Реплика открытого треда снята откатом отправки."""
        undone = self._undone_reply_ids.get(self._active_task_id, ())
        return msg_id is not None and msg_id in undone

    def _mount_turn_line(self, outcome: TurnOutcome) -> None:
        """Строка хода — на месте его записи (через `_trace_mount`, значит
        над очередью и над отправленной, ещё не записанной репликой: запись
        стопа дочитана после новой отправки). Строка последнего хода свежая
        (§3.2), если под ней нет отправленной незаписанной реплики, —
        тогда следующий ход уже начался; прошлая оседает."""
        fresh = not self._outcome_superseded and not self._sent_reply_below()
        summary = None
        stale = self._fallback_line_for(outcome.opened_by)
        if stale is not None:
            # Запись хода, у которого уже стоит строка без записи (§3.6):
            # одна строка на ход — запись заменяет её; осевшая остаётся
            # осевшей, у свежей — её сводка.
            fresh, summary = fresh and stale.is_fresh, stale.summary
            stale.remove()
        self._settle_turn_line()
        # Прошлые строки осели — видны и при стоящем баре (перерисовка
        # посреди хода): за баром ждёт только строка последнего хода.
        self._reveal_held()
        line = build_turn_line(outcome)
        # Причина записи хранится и у осевшей строки (освежить её снова).
        line.freshen(summary, outcome_error(outcome))
        if not fresh:
            line.settle()
        if self._bar_wanted():
            # Запись дочитана раньше спадающего фронта: встань строка сейчас,
            # бар ушёл бы отдельно — прыжок на 2 строки (S10 §3.4).
            line.hold()
            self._held_reveals.append(line.reveal)
        self._trace_mount(line, above_sent=True)

    def _fallback_line_for(self, opened_by: Optional[str]):
        """Строка без записи того же хода (запасной исход раннера или
        статус сессии), если после неё ход не сменился: ниже — только
        очередь и отправленное. Пустой `opened_by` у записи или у строки —
        самая новая такая строка; иначе — строка той же реплики."""
        from gc_tui.widgets.user_reply import UserReplyContainer

        trace = self.query_one(_SEL_TRACE, VerticalScroll)
        tail = self._queued_tail(trace, sent=True)
        for widget in reversed(trace.children):
            if widget in tail:
                continue
            if isinstance(widget, UserReplyContainer):
                return None
            if isinstance(widget, TurnLine):
                if widget.recorded:
                    return None
                ids = (opened_by, widget.opened_by)
                if None in ids or opened_by == widget.opened_by:
                    return widget
                return None
        return None

    def _sent_reply_below(self) -> bool:
        """В хвосте ленты — отправленная реплика, ещё не записанная ядром."""
        trace = self.query_one(_SEL_TRACE, VerticalScroll)
        return any(
            w.state == "pending" for w in self._queued_tail(trace, sent=True)
        )

    def _fresh_turn_line(self) -> Optional[TurnLine]:
        """Свежая строка последнего хода в ленте; None — её нет."""
        lines = list(self.query(TurnLine).filter(f".{TURN_LINE_LATEST}"))
        return lines[-1] if lines else None

    def _settle_turn_line(self) -> bool:
        """Начался новый ход: строка прошлого оседает в ленте. True — было
        чему оседать."""
        lines = list(self.query(TurnLine).filter(f".{TURN_LINE_LATEST}"))
        for line in lines:
            line.settle()
        return bool(lines)

    def _turn_started_after_record(self, *, live: bool) -> None:
        """Реплика за записью итога — следующий ход. Пришла хвостом посреди
        идущего прогона (ядро закрыло ход и подхватило очередь, решение
        плана 8) — у хода из очереди своя граница журнала правок."""
        settled = self._settle_turn_line()
        # Ход из очереди впущен внутри прогона: строка прошлого — осевшей в
        # ленте, бар остаётся (S10 §3.4, точка 2).
        if self._reveal_held():
            self._scroll_trace_end()
        runner = getattr(self.app, "active_runner", None)
        if (
            settled
            and live
            and runner is not None
            and runner.is_running()
            and self._runner_is_for_active_thread()
        ):
            self._edits_baseline_taken = False
            self._capture_edits_baseline()

    def _drop_turn_lines(self, msg_id: str) -> None:
        """Откат реплики `msg_id`: строка её хода уходит вместе с записью
        (запись могла быть уже дочитана хвостом и смонтирована)."""
        for line in self.query(TurnLine):
            if line.opened_by == msg_id:
                line.remove()
        last = self._last_turn_outcome
        if last is not None and last.opened_by == msg_id:
            self._last_turn_outcome = None

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
            self._thinking.show(state)
            self._sync_indicator_bar()
            return
        stream_display = self.app.base_settings().settings.tui.stream_display
        indicator = ThinkingIndicator(
            started_at=self._thinking_started_at,
            state=state,
            sink=getattr(self.app, "_stream_sink", None),
            reasoning_tag=stream_display != "answer_reasoning",
            reasoning_clock=self._reasoning_clock,
        )
        self._thinking = indicator
        self.query_one(_SEL_INDICATOR_BAR, Vertical).mount(indicator)
        self._sync_indicator_bar()
        self._follow_turn_clock()

    def _restore_latest_turn_line(self) -> None:
        """На resume сессии: освежить строку итога прошлого хода. Только
        если прогона нет и индикатор не смонтирован.

        Источник — запись итога последнего хода (исход, длительность, время,
        причина ошибки), если после неё не началась реплика; сводка правок —
        из меты того же хода. Иначе — слово статуса сессии
        (`_session_status_word`: «■ Прервано» при running без живого хода)
        строкой без времени в конце ленты; исход не записан — строки нет."""
        if (
            self._thinking is not None
            or self._store is None
            or self._active_task_id is None
        ):
            return
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and runner.is_running():
            return
        tui = self._store.get_task_meta(self._active_task_id).get("tui") or {}
        last = tui.get("last_indicator")
        edits = last.get("edits") if isinstance(last, dict) else None
        outcome = self._latest_turn_outcome()
        if outcome is not None:
            word, plan = kind_of(outcome)
        else:
            # Строка последней записи в ленте — не итог последнего хода.
            self._settle_turn_line()
            word = word_of_status(self._session_status_word())
            plan = False
            if word is None:
                return
        self._show_turn_outcome(word, plan, outcome, edits=edits)

    def _latest_turn_outcome(self) -> Optional[TurnOutcome]:
        """Запись итога последнего хода открытого треда; после неё началась
        реплика (ход идёт или оборван без записи) — None. Ход оборван без
        записи (статус «running» без живого хода: процесс убит) — тоже
        None: запись — прошлого хода, даже если реплики после неё нет
        (континуация плана открывается технической)."""
        if self._outcome_superseded:
            return None
        if self._session_status_word() == "interrupted":
            return None
        return self._last_turn_outcome

    def _show_turn_outcome(
        self,
        word: str,
        plan: bool,
        outcome: Optional[TurnOutcome],
        *,
        edits=None,
    ) -> None:
        """Итог последнего хода — свежая строка в ленте, индикатор уходит.

        Есть запись итога — освежается её строка: сводка правок `edits`
        (не задана — сводка закрытого хода), причина ошибки из записи. Без
        записи (§3.6) — строка без времени по слову `word` в конец ленты,
        причина — живая ошибка раннера; одна на ход: повторный вызов
        освежает её же."""
        self._destroy_thinking_indicator()
        summary = self._restored_turn_summary(
            self._closed_turn_summary if edits is None else edits
        )
        line = self._fresh_turn_line()
        if outcome is not None:
            if line is not None:
                line.freshen(summary, outcome_error(outcome))
            return
        error = self._active_run_error() if word == "failed" else None
        if line is None or line.recorded:
            self._settle_turn_line()
            line = build_turn_line(
                word=word, plan=plan, opened_by=self._last_user_message_id
            )
            line.freshen(summary, error)
            self._trace_mount(line, above_sent=True)
            return
        line.freshen(summary, error)

    def _hide_thinking_indicator(self) -> None:
        """Скрыть индикатор (CSS `.hidden`), не удаляя; анимация на паузе."""
        if self._thinking is None:
            return
        self._thinking.hide()
        self._sync_indicator_bar()

    def _destroy_thinking_indicator(self) -> None:
        """Полностью убрать индикатор. Вызывается когда run завершён."""
        if self._thinking is None:
            return
        self._thinking.remove()
        self._thinking = None
        self._sync_indicator_bar()

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
        from gc_tui.services.markdown_render import render_prose
        from gc_tui.services.stream_sink import (
            format_reasoning_line,
            format_stream_block,
        )

        self._update_stick_state()
        think, answer = sink.snapshot()
        if self._stream_answer_row is not None:
            self._stream_answer_row.set_content(
                render_prose(format_stream_block(answer=answer))
            )
        elif self._stream_block is not None:
            self._stream_block.set_text(
                format_reasoning_line(think=think), thinking=True
            )
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
        return (
            self._ask_human_banner or self._confirm_banner or self._plan_banner
        )

    def _mount_sticky_banner(self, banner, inp_row: Horizontal) -> None:
        """Баннер — в `#root` над полем ввода. Бар уходит, когда баннер
        смонтирован (§4.12): его ряды появляются в той же раскладке, что
        и уход бара, — без кадра, где нет ни того, ни другого."""
        mounted = self.query_one(_SEL_ROOT, Vertical).mount(
            banner, before=inp_row
        )

        async def _sync_bar_when_mounted() -> None:
            await mounted
            self._sync_indicator_bar()

        self.call_later(_sync_bar_when_mounted)
        self.call_after_refresh(self._sync_trace_max_height)

    def _cap_sticky_banner(self, banner, *, chrome: int) -> int:
        """Потолок sticky-баннера: длинный вопрос не выдавливает input/footer.

        Как у input-оверлея — ≤ половины экрана и оставляет min строк трейса.
        Возвращает желаемую (уже ужатую) высоту баннера для расчёта #trace.

        ConfirmBanner (docked Y/O/N/T + scroll body): одного max_height мало —
        Textual клипает box, а дети layout'ятся полной высотой и опции уезжают
        за клип. При overflow ставим height=cap, чтобы dock/scroll работали
        внутри реальной высоты (см. needs_height_when_capped)."""
        cap = min(
            self.size.height // 2,
            self.size.height - chrome - self._MIN_TRACE_WITH_OVERLAY,
        )
        if cap <= 0:
            return 0
        banner.styles.max_height = cap
        # После layout region уже включает round-border и ≤ cap — самый точный замер.
        # До layout / пока region ещё не ужат: virtual_size = контент без рамки (+2),
        # иначе chrome занижается на 2 и футер уезжает за край экрана.
        try:
            content_h = banner.virtual_size.height
        except Exception:
            content_h = 0
        desired = getattr(banner, "desired_height", None)
        if isinstance(desired, int) and desired > 0:
            estimated = desired
        elif content_h > 0:
            estimated = content_h + 2
        else:
            estimated = len(banner.children) + 2
        # Force real height when content exceeds cap and banner needs it
        # (ConfirmBanner docked options). AskHuman is VerticalScroll — max_height
        # alone is enough; clearing height keeps hug-content behaviour.
        if getattr(banner, "needs_height_when_capped", False):
            capped = estimated > cap
            if capped:
                banner.styles.height = cap
            else:
                banner.styles.height = None  # auto / hug content
            # Тело растягиваем только при зафиксированной высоте: иначе
            # height:1fr у ребёнка отменяет hug-content у родителя.
            hug = getattr(banner, "set_hug_content", None)
            if callable(hug):
                hug(not capped)
        region_h = banner.region.height
        if 0 < region_h <= cap:
            return region_h
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
            root = self.query_one(_SEL_ROOT, Vertical)
            trace = self.query_one(_SEL_TRACE, VerticalScroll)
        except Exception:
            return
        overlay = self._open_overlay_widget()
        banner = self._sticky_banner_widget()
        # region.height включает бордеры; скрытые виджеты (display:none) дают 0.
        # Баннер/оверлей исключаем — их желаемую высоту считаем отдельно (иначе
        # ещё не ужатый region баннера раздувает chrome до available<=0).
        # Бар — константой по классу, не замером: потолок пересчитывается в
        # том же вызове, где бар ставят или снимают (S10 §5).
        chrome = _TRACE_GAP + sum(
            self._chrome_height(c)
            for c in root.children
            if c.id != "trace" and c is not overlay and c is not banner
        )
        banner_h = 0
        if banner is not None:
            banner_h = self._cap_sticky_banner(banner, chrome=chrome)
        chrome_with_banner = chrome + banner_h
        if overlay is not None:
            # Оверлей ≤ половины экрана (дальше — его скролл) и оставляет ≥ min строк
            # трейса. На малых экранах вторая граница жёстче, на больших — половина.
            cap = min(
                self.size.height // 2,
                self.size.height
                - chrome_with_banner
                - self._MIN_TRACE_WITH_OVERLAY,
            )
            if cap <= 0:
                return
            overlay.styles.max_height = (
                cap  # потолок оверлея → дальше его скролл
            )
            # Желаемая высота: все наши оверлеи (CommandPalette, SkillPicker,
            # ArgPicker, FilePicker) отдают честный `desired_height` (дети +
            # margin hint'а + рамка; у палитры — ещё докнутый подвал). Фолбэк
            # `len(children)+2` — только для гипотетического оверлея без свойства.
            # len(children) обновляется при фильтре сразу, а virtual_size залипает.
            desired = min(
                getattr(overlay, "desired_height", len(overlay.children) + 2),
                cap,
            )
            available = self.size.height - chrome_with_banner - desired
        else:
            available = self.size.height - chrome_with_banner
        if available <= 0:
            return
        if available != self._trace_max_height:
            self._trace_max_height = available
            trace.styles.max_height = available
            # Ряд плашки считается от потолка: она переезжает в той же
            # перерисовке, что и лента с баром (S10 §4.2).
            self._refresh_trace_overlays()

    @staticmethod
    def _chrome_height(widget) -> int:
        if widget.id == _SEL_INDICATOR_BAR[1:]:
            return _BAR_HEIGHT if widget.has_class(_BAR_TURN) else 0
        return widget.region.height

    _STICK_TOL = 2

    def _trace_is_at_bottom(self) -> bool:
        """Сырой замер: трейс прокручен в самый низ (с допуском). Используется в
        _update_stick_state для повторного «прилипания», когда юзер вернулся к низу."""
        try:
            trace = self.query_one(_SEL_TRACE, VerticalScroll)
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
            trace = self.query_one(_SEL_TRACE, VerticalScroll)
        except Exception:
            return
        if self._stick_bottom:
            # Низ поднялся (лента выросла в высоту) — offset прижат к нему,
            # это не уход вверх: порог — не выше нынешнего низа.
            pinned = min(self._last_auto_y, trace.max_scroll_y)
            if trace.scroll_offset.y < pinned - self._STICK_TOL:
                self._stick_bottom = False
        elif self._trace_is_at_bottom():
            self._stick_bottom = True

    def _on_trace_scrolled(self, _value: float) -> None:
        """Отклик на прокрутку ленты по событию, не по опросу. Прижатие
        остаётся в _poll_tick; наш scroll_end липкость не снимает (offset не
        меньше _last_auto_y). Во время выделения (F2) — ни детекта, ни
        прижатия."""
        if self._ui_frozen_for_select():
            return
        self._update_stick_state()
        self._refresh_trace_overlays()

    def _refresh_trace_overlays(self) -> None:
        """Накладки над лентой (спека P3 §2.6): «не внизу» — липкая реплика
        (ближайшая выше края) и плашка «в конец»; внизу — обе скрыты.
        Зовётся на каждый шаг прокрутки: реплики ищутся по кэшу верхних
        реплик (`_trace_replies`), без обхода ленты."""
        try:
            trace = self.query_one(_SEL_TRACE, VerticalScroll)
            sticky = self.query_one("#trace-sticky-reply", StickyReply)
            pill = self.query_one("#trace-end-pill", ScrollEndPill)
        except NoMatches:
            return
        # Коробка накладок — ширина подложки реплики в ленте: без колонки
        # скроллбара (решение 27). Плашка центруется по ней же.
        box = trace.scrollable_content_region
        if self._stick_bottom:
            self._reset_new_replies()
            sticky.show(None, box)
            pill.show(None, box)
            return
        replies = self._trace_replies(trace)
        reply = reply_above(
            replies, round(trace.scroll_y), lambda r: r.virtual_region.y
        )
        sticky.show(reply, box)
        pill.show(self._end_pill_label(), box, self._pill_row(trace))

    def _pill_row(self, trace: VerticalScroll) -> int:
        """Ряд плашки «в конец» (S10 §4.2) — над баннером или полем ввода:
        пока бар стоит — его нижний ряд, под индикатором хода; без бара —
        нижний зазор ленты. Поле ввода баннеры прячут, поэтому опора — не
        оно. Плашка видна только у прокручиваемой ленты, её высота — потолок;
        ряд считается от него, а не от `region` прошлой раскладки: баннер,
        сменивший потолок, не застанет плашку на своей строке."""
        bar = self.query_one(_SEL_INDICATOR_BAR, Vertical)
        lift = _TRACE_GAP + _BAR_HEIGHT - 1 if bar.has_class(_BAR_TURN) else 0
        if self._trace_max_height is None:
            return trace.region.bottom + lift
        return trace.region.y + self._trace_max_height + lift

    def _trace_replies(self, trace: VerticalScroll) -> list:
        """Реплики верхнего уровня ленты по порядку. Кэшируется только
        состав; верх каждой читается живым при поиске, поэтому сдвиг
        реплик кэш не портит. Ключ — число детей и первый с последним:
        точный ключ (все дети) стоил бы столько же, сколько пересборка;
        этот ловит и снятие с монтированием в конец за один такт."""
        from gc_tui.widgets.user_reply import UserReplyContainer

        children = trace.children
        key = (len(children), children[0], children[-1]) if children else ()
        if key != self._trace_replies_key:
            self._trace_replies_key = key
            self._trace_replies_cache = [
                child
                for child in trace.children
                if isinstance(child, UserReplyContainer)
            ]
        return self._trace_replies_cache

    def _count_new_reply(self, following: bool) -> bool:
        """Реплика агента смонтирована: не у низа — она новая для плашки.
        True — посчитана."""
        if following:
            return False
        self._new_replies += 1
        self._refresh_trace_overlays()
        return True

    def _reset_new_replies(self) -> None:
        self._new_replies = 0
        self._new_attempt_replies = 0

    def _end_pill_label(self) -> str:
        """`· клик или Enter` — только когда курсор в пустом поле ввода.
        Пока окно терминала не в фокусе, поле ввода снято с фокуса самим
        Textual: хвост не пересчитываем, подпись не мигает."""
        if self.app.app_focus:
            try:
                inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
            except NoMatches:
                self._pill_enter = False
            else:
                self._pill_enter = self.focused is inp and inp.is_blank()
        return pill_label(self._new_replies, enter=self._pill_enter)

    def _on_focus_moved_overlays(self, _focused: Optional[Widget]) -> None:
        self._refresh_trace_overlays()

    def on_scroll_to_reply(self, message: ScrollToReply) -> None:
        """Клик по липкой реплике — она наверху видимой области."""
        if not message.reply.is_attached:
            return  # реплику успели снять
        trace = self.query_one(_SEL_TRACE, VerticalScroll)
        trace.scroll_to_widget(message.reply, animate=False, top=True)

    def on_scroll_to_end(self, _message: ScrollToEnd) -> None:
        """Клик по плашке «в конец» — тот же путь, что Enter в пустом поле."""
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        """Одно действие «к низу» для Enter в пустом поле и клика по плашке."""
        self._stick_bottom = True
        self._scroll_trace_end()

    def _scroll_trace_end(self) -> None:
        """Прижать трейс к низу ПОСЛЕ раскладки (height:auto → scroll_end сразу после
        mount() не достаёт до нового низа) и запомнить позицию для _update_stick_state.

        Отсрочки две: экрана и самой ленты (та же, что у `scroll_end`
        Textual, — новый низ готов только после неё). Флаг проверяется
        перед каждой: ушедшего вверх между заказом и прокруткой назад не
        тянем; законные прижатия ставят флаг до заказа."""

        def _trace() -> Optional[VerticalScroll]:
            if not self._stick_bottom or self._ui_frozen_for_select():
                return None
            try:
                return self.query_one(_SEL_TRACE, VerticalScroll)
            except Exception:
                return None

        def _end() -> None:
            trace = _trace()
            if trace is not None:
                self._last_auto_y = trace.max_scroll_y
                trace.scroll_end(animate=False, immediate=True)

        def _do() -> None:
            trace = _trace()
            if trace is not None:
                trace.call_after_refresh(_end)

        self.call_after_refresh(_do)

    def _trace_mount(
        self,
        widget,
        *,
        scroll: bool = True,
        force_scroll: bool = False,
        above_sent: bool = False,
        close_groups: bool = True,
    ) -> None:
        """Смонтировать widget в #trace (индикатор живёт отдельно в #indicator-bar).

        scroll=True: прокрутить вниз, но только если юзер уже внизу
        (`_trace_is_at_bottom`) — иначе не дёргаем его прокрутку. force_scroll=True
        прокручивает безусловно (отправка своего сообщения = re-engage).

        Очередь прижата к низу (спека P3 §2.4): всё новое встаёт перед первой
        репликой в очереди, сама такая реплика — в конец. Единственное место
        этого правила: всё, что монтируется в конец ленты, идёт сюда.

        above_sent=True — запись о прошлом (итог хода): встаёт и над
        отправленной репликой, которую ядро ещё не записало (`pending`), —
        в файле треда она будет после.

        close_groups=False — виджет не событие верхнего уровня (вложенный
        без якоря): группы блоков он не закрывает."""
        from gc_tui.widgets.user_reply import is_queued

        if close_groups:
            self._note_top_event(widget)
        trace = self.query_one(_SEL_TRACE, VerticalScroll)
        if force_scroll:
            self._stick_bottom = True  # отправка своего сообщения = re-engage
        queue = (
            []
            if is_queued(widget)
            else self._queued_tail(trace, sent=above_sent)
        )
        if queue:
            trace.mount(widget, before=queue[0])
        else:
            trace.mount(widget)
        if scroll and self._stick_bottom:
            self._scroll_trace_end()

    def _note_top_event(self, widget) -> None:
        """Единственная точка закрытия групп верхнего уровня (решение
        плана 10): событие ленты не из группы закрывает открытую группу.
        Не закрывают: виджеты модели блоков, живой стрим, реплика в
        очереди."""
        from gc_tui.widgets.user_reply import is_queued

        view = self._block_views.get("")
        if view is None or view.issued(widget) or is_queued(widget):
            return
        if widget is self._stream_answer_row or widget is self._stream_block:
            return
        view.event_closing()

    @staticmethod
    def _queued_tail(trace, *, sent: bool = False) -> list:
        """Реплики очереди по порядку. Очередь — всегда сплошной хвост
        ленты (`_trace_mount`), поэтому обход с конца, а не всей ленты.
        ``sent`` — с ними и отправленные, ещё не записанные ядром."""
        from gc_tui.widgets.user_reply import UserReplyContainer, is_queued

        tail = []
        for widget in reversed(trace.children):
            sent_reply = (
                sent
                and isinstance(widget, UserReplyContainer)
                and widget.state == "pending"
            )
            if not (is_queued(widget) or sent_reply):
                break
            tail.append(widget)
        return tail[::-1]

    def _withdraw_last_queued(self) -> bool:
        """↑ забирает последнюю реплику очереди в поле (спека P3 §2.4, §10
        ответ 2): поле в фокусе и пустое, палитра и пикеры закрыты, история
        не листается, очередь есть. Иначе `False` — ↑ как раньше. Впущенное
        ядром не забирается: подсказка в футере, реплика остаётся (сверка
        сделает её `normal`). Клавиша съедена в обоих случаях."""
        if self._any_picker_open or self._history_index is not None:
            return False
        inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
        if not (inp.has_focus and inp.is_blank()):
            return False
        runner = self.app.active_runner
        tail = self._queued_tail(self.query_one(_SEL_TRACE, VerticalScroll))
        if runner is None or not tail:
            return False
        reply = tail[-1]
        if not runner.withdraw_message(reply.message_id):
            self._show_footer_refusal(QUEUE_ADMITTED_HINT)
            return True
        self._pending_user_widgets.pop(reply.client_id, None)
        self._remove_pending_record(reply.client_id)
        self._restore_input(reply.plain_text, reply.images)
        self.call_next(self._drop_queued_reply, reply)
        return True

    def _queued_replies(self) -> list:
        """Реплики очереди по порядку отправки (словарь pending хранит
        порядок вставки; забранная ↑ из него уже вынута)."""
        from gc_tui.widgets.user_reply import is_queued

        return [w for w in self._pending_user_widgets.values() if is_queued(w)]

    def _withdraw_queue(self, runner) -> list:
        """Снять очередь у ядра (стоп, конец хода): снятые — вернуть в
        поле (`_return_queue_to_input`); не снятое ядро уже взяло или
        возьмёт — реплика `pending`, без вида очереди, её сверит тред."""
        withdrawn = []
        for reply in self._queued_replies():
            if runner.withdraw_message(reply.message_id):
                withdrawn.append(reply)
            else:
                reply.set_state("pending")
                self._held_client_ids.add(reply.client_id)
        return withdrawn

    def _has_held_replies(self) -> bool:
        """Есть реплика, которую ядро держит после стопа или конца хода,
        и тред её ещё не сверил."""
        return any(
            cid in self._held_client_ids for cid in self._pending_user_widgets
        )

    def _return_queue_to_input(self, replies: list) -> None:
        """Реплики, которые ядро уже не впустит (снятые при стопе, остаток
        в конце хода), — назад в поле: тексты по порядку очереди через
        пустую строку (`/имя` — как в реплике), с картинками; набранное в
        поле остаётся впереди. Реплики снимаются из ленты и
        `meta.tui.pending_user_messages`."""
        if not replies:
            return
        inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
        parts = [inp.text] if inp.text else []
        images: list[Attachment] = []
        for reply in replies:
            parts.append(reply.plain_text)
            images.extend(reply.images)
            self._pending_user_widgets.pop(reply.client_id, None)
            self._remove_pending_record(reply.client_id)
            reply.remove()
        self._restore_input("\n\n".join(parts), tuple(images))

    async def _drop_queued_reply(self, reply) -> None:
        """Снять забранную реплику; подсказка — новой последней. Удаление
        в Textual отложенное: до его конца реплика ещё в хвосте очереди."""
        await reply.remove()
        self._refresh_queue_hint()

    def _refresh_queue_hint(self) -> None:
        """Подсказка очереди — только под последней ожидающей репликой
        (спека P3 §10). Вне очереди подсказку гасит сама реплика
        (`set_state`)."""
        tail = self._queued_tail(self.query_one(_SEL_TRACE, VerticalScroll))
        for reply in tail:
            reply.set_queue_hint(reply is tail[-1])

    def _on_user_text_submitted(
        self, text: str, images: tuple[Attachment, ...] = ()
    ) -> None:
        """Обработка отправки user query: обнулить счётчик событий с последнего
        user-send и запомнить текст для cancel-with-undo (Tasks 9-10).
        Дополнительно: append текста в input-history и выход из history-навигации.

        R6 (in-session-rename): авто-имя сессии. normalize → seed/update
        ``last_actual`` в ``tui_meta`` (read-merge-write через стор). Первый
        промпт сеет безусловно; далее — только если ``len(norm) > LAST_ACTUAL_MIN``.
        **Порядок:** запись meta → затем ``_update_terminal_title`` (OSC видит
        свежее имя). best-effort (try/except). ``_resumed_prompt`` остаётся сырым
        для attach/cancel — это НЕ имя сессии (R5: OSC/пикер читают display_name).

        Картинки (дизайн картинок §12): подписи `[Изображение #N]` в историю ↑ и
        имя сессии не идут — вспомненная подпись без записи в сторе была бы мёртвой
        плашкой."""
        self.spend_help_hint()
        plain = strip_image_chips(text, images)
        if self._plan_rework_pending:
            self._plan_rework_pending = False
            # Вне plan статус плана не сменится — строка была бы неправдой.
            if self.app.effective_permission_mode() == Mode.PLAN.value:
                self._mount_trace_row(
                    prefix="· ",
                    content="план отправлен на доработку",
                    kind_class="line-meta",
                )
            self._sync_input_placeholder()
        self._resumed_prompt = text  # send = новое последнее сообщение активной сессии (сырой текст)
        self._seed_or_update_last_actual(plain)
        self._update_terminal_title()
        if self._history is not None:
            self._history.append(plain)
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
                    self._write_tui_meta(
                        self._active_task_id, {"last_actual": norm}
                    )
            else:
                # seed: первый промпт безусловно
                self._write_tui_meta(
                    self._active_task_id, {"last_actual": norm}
                )
        except Exception:
            pass

    def _seed_or_update_task_from_submit(self, ev) -> None:
        """Записать ``meta.tui.task`` из arguments turn_finish и обновить OSC.

        Эвристика (should_update_task): первый task сеется безусловно; дальше
        имя обновляет только ход с реальной работой (``_live_turn_tool_calls``),
        чтобы «спасибо»/«пока» не переименовывали сессию. Совпадающее значение
        не перезаписывается (no-op guard)."""
        if self._active_task_id is None or self._store is None:
            return
        payload = ev.full_payload if isinstance(ev.full_payload, dict) else {}
        args = coerce_args(payload.get("arguments"))
        task = task_title({"task": args.get("task")})
        if not task:
            return
        try:
            meta = self._store.get_task_meta(self._active_task_id) or {}
        except Exception:
            return
        tui_meta = meta.get("tui") or {}
        if not should_update_task(tui_meta, self._live_turn_tool_calls):
            return
        if task_title(tui_meta) == task:
            return
        try:
            self._write_tui_meta(self._active_task_id, {"task": task})
        except Exception:
            return
        self._update_terminal_title()

    def _update_terminal_title(self) -> None:
        """Заголовок терминала = имя сессии + текущий статус (для таба/таскбара).

        R5 (in-session-rename): промпт = display_name(meta.tui) (title > task >
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
            return display_name(self._tick_meta().get("tui"))
        except Exception:
            return self._resumed_prompt or ""

    def _rename_active_session(self) -> None:
        """R2 (in-session-rename): открыть RenameScreen(current=display_name) активной сессии.

        Гейт: ``_active_task_id`` и ``_store`` не None. Текущее имя =
        ``_session_display_name()`` (title > task > last_actual > «Без названия»).
        Результат → ``_on_rename_result`` (R3).
        """
        if self._active_task_id is None or self._store is None:
            return
        from gc_tui.screens.rename import RenameScreen

        current = self._session_display_name()
        self.app.push_screen(
            RenameScreen(current=current), self._on_rename_result
        )

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
            self._write_tui_meta(self._active_task_id, {"title": value})
        except Exception:
            return
        self._update_terminal_title()

    def _session_last_prompt(self) -> Optional[str]:
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
            if (
                rec.action == ACTION_USER_ASK
                and isinstance(rec.content, str)
                and rec.content.strip()
            ):
                return rec.content
        for s in reversed(meta.get("user_messages_log") or []):
            if isinstance(s, str) and s.strip():
                return s
        return None

    def _reset_thinking_timer(self) -> None:
        """Сбросить таймер «думаю…»: новая точка отсчёта = монотоника сейчас,
        текущий виджет удалён (следующий show создаст свежий с актуальным started_at).
        Также сбрасываем вид последнего события — после send юзер ждёт
        ответ заново.
        Вызывается на каждом user-send (3 пути отправки)."""
        import time as _time

        now = _time.monotonic()
        self._thinking_started_at = now
        self._last_event_kind = None
        self._reasoning_clock.reset()
        self._destroy_thinking_indicator()

    def _render_event(self, ev, *, live: bool = False) -> None:
        if ev.hidden:
            return

        # Any committed canonical row supersedes the ephemeral reasoning/answer
        # preview for the current step — clear it so it can't drift above new rows.
        # (It re-mounts at the bottom on the next poll tick if streaming continues.)
        self._clear_stream_block()

        # ask_human-output banner cleanup — Фикс B-2.
        # Mapper эмитит для function_call_output name=ask_human либо kind='user'
        # (непустой response), либо kind='system' (пустой/cancel/broken). В обоих
        # случаях снимаем banner ДО передачи в kind-handler, чтобы kind='user'
        # затем нормально реконсилил pending через _pending_wire_match,
        # а kind='system' дошёл до default-ветки: «└ без ответа» под вопросом.
        if (
            self._ask_human_call_id is not None
            and ev.tool_name == "ask_human"
            and ev.kind in ("user", "system")
        ):
            ah_match = (
                not ev.call_id
            ) or ev.call_id == self._ask_human_call_id
            if ah_match:
                self._unmount_ask_human_banner()
                # НЕ return — событие должно дойти до своего обработчика.

        if ev.kind == "user":
            # New conversation turn — clear retry-tracking from previous turn(s).
            # Previous submit_solutions are now considered completed answers,
            # not retry candidates for collapse.
            self._submit_solution_widgets.clear()
            self._new_attempt_replies = 0
            # Дедуп A1 действует внутри хода: проза прошлого хода не
            # глушит такой же ответ этого.
            self._answer_flow.new_turn()
            if live:
                # Граница хода для эвристики имени — именно user-запись ядра:
                # она есть и при обычном старте, и при подхвате очереди бегущим
                # раннером как нового хода (Enter в TUI там не через
                # _prepare_for_new_run). Ответ на ask_human user-записи не даёт.
                self._live_turn_tool_calls = 0

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
                    self._top_blocks().queued_admitted()
                    widget.set_event_no(ev.no)
                    # Картинки из треда — копии, закоммиченные ядром; у pending-реплики
                    # пути кармана процесса, который чистится на выходе (дизайн картинок §3).
                    if ev.attachments:
                        widget.set_images(ev.attachments)
                    self._pending_user_widgets.pop(cid, None)
                    self._remove_pending_record(cid)
                    self._refresh_queue_hint()
                    return
            # Без pending-матча — обычный mount (используем display_content = ev.summary).
            self._mount_user_reply(
                ev.summary,
                event_no=ev.no,
                images=ev.attachments,
                skill=ev.skill,
            )
            return

        if ev.kind == "agent":
            # Метаданные финализатора обрабатываем независимо от визуального
            # дедупа ниже: task_report может совпасть с предыдущей agent-прозой.
            # Без гейта на live: перерисовка после компакции (stream_reset) —
            # единственный путь финализатора, если poll-тик не успел до неё;
            # от записей на реплее защищает should_update_task (счётчик live-only).
            if ev.tool_name == "submit_solution":
                self._seed_or_update_task_from_submit(ev)
                self._render_finish(ev)
                return
            # A1: дубль контента (проза → тот же финализатор / повтор) — молча
            # опускаем второй ряд (контент уже показан первым). Exact-match (strip),
            # без маркера: завершение хода и так видно по готовому инпуту.
            verdict = self._answer_flow.agent(
                (ev.summary or "").strip(),
                intermediate=ev.tool_name == "intermediate_reply",
            )
            if not verdict.show:
                return
            if verdict.drop_attempt:
                # Ответ модели повторил отчёт отклонённой попытки: попытка
                # уходит, текст остаётся ответом — свёртка не унесёт его.
                self._drop_finish_attempts()
            if ev.tool_name == "intermediate_reply":
                from gc_tui.services.trace_render import (
                    build_intermediate_widgets,
                )

                # «Прилипание к низу»: фиксируем ДО монтирования (как у прозы).
                following = self._stick_bottom
                for w in build_intermediate_widgets(
                    ev.summary, event_no=ev.no
                ):
                    self._trace_mount(w, scroll=False)
                self._count_new_reply(following)
                if following:
                    self._scroll_trace_end()
                return
            from gc_tui.services.trace_render import (
                build_agent_widgets,
                build_block_widgets,
            )

            # «Прилипание к низу»: фиксируем ДО монтирования (mount растит max_scroll_y).
            following = self._stick_bottom
            is_plan = ev.tool_name == PROPOSE_PLAN_ID
            mounted_widgets = (
                # В рамке с подписью маркер ответа «●» лишний.
                build_block_widgets(
                    ev.summary, prose_bullet=False, event_no=ev.no
                )
                if is_plan
                else build_agent_widgets(ev.summary, event_no=ev.no)
            )
            if is_plan:
                # План — в рамке с подписью: он не обычный ответ, его одобряют.
                self._trace_mount(
                    PlanPanel(*mounted_widgets, plan_text=ev.summary),
                    scroll=False,
                )
            else:
                for w in mounted_widgets:
                    self._trace_mount(w, scroll=False)
            self._count_new_reply(following)
            if following:
                self._scroll_trace_end()
            return

        if ev.kind == "ask_human":
            self._on_ask_human_event(ev, live=live)
            return

        if ev.kind == "todo":
            # Чек-лист — пассивный прогресс: TraceRow без running-глифа/result.
            # Когнитивное событие — эпизод раздумья не рвём (как session_think).
            # Канон spec 2026-07-31 §2 — строки собирает общий билдер
            # (он же у read-only канала), здесь только монтируем.
            from gc_core.execution.progress import (
                todo_items_from_arguments,
            )

            from gc_tui.services.todo_render import build_todo_block

            items = todo_items_from_arguments(
                (ev.full_payload or {}).get("arguments")
            )
            self._trace_mount(
                build_todo_block(
                    items, subagent=False, indent="", event_no=ev.no
                )
            )
            return

        if ev.kind == "call":
            if live and ev.tool_name not in COGNITIVE_TOOLS:
                # Работа хода для эвристики имени — только действие в мире
                # (когнитивные тулы — не работа; финализатор и intermediate_reply
                # приходят kind="agent" и сюда не попадают).
                self._live_turn_tool_calls += 1
            if ev.tool_name in COGNITIVE_TOOLS:
                # Когнитивный тул продолжает эпизод (без call_id — просто no-op,
                # НЕ рвёт эпизод и не попадает в interrupt).
                if ev.call_id:
                    self._reasoning_clock.start(ev.call_id)
            elif ev.tool_name:
                # Не-когнитивный тул = действие в мире → рвёт эпизод раздумья.
                self._reasoning_clock.interrupt()
            mcp_tools, mcp_servers = self._mcp_tool_info()
            wire_name = ev.wire_name or ev.tool_name or "<tool>"
            # Вживую строка ждёт очереди батча (`•`), пока исполнение до неё
            # не дошло; в перерисовке старта вживую не было — не ждёт.
            row = build_call_row(
                call_id=ev.call_id or f"_anon_{ev.no}",
                wire_name=wire_name,
                args_summary=ev.args_summary,
                level=0,
                event_no=ev.no,
                waiting=live,
                mcp_tools=mcp_tools,
                mcp_servers=mcp_servers,
                agent=ev.agent,
            )
            self._active_calls[row.call_id] = row
            # Durable anchor for nested (reopen / after parent result pop).
            if (ev.tool_name or "").startswith("subagent_") and row.call_id:
                self._subagent_parent_rows[row.call_id] = row
            category, _ = tool_caption(wire_name, mcp_tools, mcp_servers)
            self._top_blocks().add_call(
                row, category, waiting=live, command=ev.command_lines
            )
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
                edit, compact = self._block_edit(ev.call_id)
                view = self._top_blocks()
                ran = None
                if view.model.kind_of(row.call_id) == BlockKind.RAN:
                    ran = self._ran_result(ev)
                view.result(row, ev, edit, compact=compact, ran=ran)
                # Nested под subagent_* оставляем в живой сессии (иерархия под ✓/✕).
                self._active_calls.pop(ev.call_id, None)
                self._reconcile_active_tool_phases()
                return
            # Fallback (call_id unknown or no matching row) — append separate line
            if ev.kind == "error":
                from gc_tui.services.trace_render import (
                    build_orphan_result_row,
                )

                self._trace_mount(
                    build_orphan_result_row(
                        ev.summary, ev.result_outcome, event_no=ev.no
                    )
                )
                return
            self._mount_trace_row(
                prefix="   ",
                content=ev.summary,
                kind_class="line-ok",
                event_no=ev.no,
            )
            return

        if ev.kind == "summary":
            marker, body = build_compaction_widgets(ev, focusable=True)
            self._trace_mount(marker)
            self._summary_marker_widgets[ev.no] = marker
            if body is not None:
                # Свёрнуто по умолчанию; toggle по клику метки ИЛИ тела.
                self._trace_mount(body)
                self._summary_body_widgets[ev.no] = body
            return

        if ev.kind == "error_msg":
            self._render_error_msg(ev)
            return

        if ev.tool_name == "ask_human" and self._note_unanswered(ev.call_id):
            return
        # default: meta. summary вида "[tag] описание" — отрезаем prefix для TraceRow.
        from gc_tui.services.trace_render import TAG_RE

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
                prefix="",
                content=ev.summary,
                kind_class="line-meta",
                event_no=ev.no,
            )

    def _note_unanswered(self, call_id: str | None) -> bool:
        """«└ без ответа» под вопросом `call_id` в его рамке. Вопроса в ленте
        нет (ушёл в сжатие) — False: событие рисуется строкой, как было."""
        from gc_tui.services.trace_render import (
            QUESTION_UNANSWERED,
            note_question,
        )
        from gc_tui.widgets.trace_row import QuestionRow

        if not call_id:
            return False
        trace = self.query_one(_SEL_TRACE, VerticalScroll)
        for question in trace.query(QuestionRow):
            if question.call_id == call_id:
                note_question(question, QUESTION_UNANSWERED)
                return True
        return False

    def _render_error_msg(self, ev) -> None:
        """error_msg с известным call_id финализирует соответствующую строку-вызов
        (✕ inline, таймер стоп) и подавляет дублирующую строку. Иначе — строка
        `!` без владельца."""
        row = self._active_calls.get(ev.call_id) if ev.call_id else None
        if row is not None:
            self._top_blocks().result(row, replace(ev, outcome="error"), None)
            # Nested под subagent_* оставляем в живой сессии (иерархия под ✓/✕).
            self._active_calls.pop(ev.call_id, None)
            self._reconcile_active_tool_phases()
            return
        self._mount_standalone(ev.summary, "error", event_no=ev.no)

    def _reconcile_active_tool_phases(self) -> None:
        """Батч исполняется по порядку: первая незавершённая строка, если
        ещё ждёт, — стартовала (через модель блоков)."""
        row = first_pending(list(self._active_calls.values()))
        if row is not None and row.waiting:
            self._top_blocks().started(row)

    def _top_blocks(self) -> ToolBlocksView:
        """Исполнитель блоков верхнего уровня ленты; после сброса ленты —
        новый, с новой моделью."""
        view = self._block_views.get("")
        if view is None:
            model = ToolBlocks(
                self._call_timers,
                diff_preview=self._trace_inline_max_lines(),
            )
            view = ToolBlocksView(
                _TraceBlockHost(self), model, level="", depth=0
            )
            self._block_views[""] = view
        return view

    def _mount_block_widget(self, widget, *, before=None, after=None) -> None:
        """Виджет блока: в конец ленты — по правилу очереди, рядом со своим
        виджетом — прямо (якорь — прямой ребёнок `#trace`)."""
        if before is None and after is None:
            self._trace_mount(widget)
            return
        trace = self.query_one(_SEL_TRACE, VerticalScroll)
        trace.mount(widget, before=before, after=after)
        if self._stick_bottom:
            self._scroll_trace_end()

    def _block_edit(
        self, call_id: Optional[str]
    ) -> tuple[Optional[BlockEdit], bool]:
        """Правка вызова из журнала и «ход вне окна раскрытых» (компактный
        дифф). Сбой журнала — без правки: лента важнее диффа."""
        try:
            return call_edit(
                self._edits_lookup_for_thread(),
                call_id or "",
                window=self._replay_expanded_turns(),
            )
        except Exception as exc:
            self.log.warning(f"правка вызова не прочитана: {exc!r}")
            return None, False

    def _ran_result(self, ev) -> RanResult:
        """Законченная команда: вывод из события и правки команды из
        журнала. Сбой журнала — без правок: лента важнее."""
        try:
            return ran_result_of(
                self._edits_lookup_for_thread(),
                ev,
                window=self._replay_expanded_turns(),
            )
        except Exception as exc:
            self.log.warning(f"правки команды не прочитаны: {exc!r}")
            return RanResult(output=ev.command, light=is_light_payload(ev))

    def _end_turn_blocks(self, outcome: TurnOutcome) -> None:
        """Итог хода — моделям блоков всех уровней (таймеры, стоп)."""
        end_turn(self._block_views, self._call_timers, outcome, settled=False)

    def on_block_toggle(self, message: BlockToggle) -> None:
        """Раскрыть / свернуть блок (§9.3): внизу — остаться внизу; иначе
        после перекладки довести верх блока до видимости."""
        message.stop()
        at_bottom = self._stick_bottom
        routed = self._block_views.route(message)
        if routed is None:
            return
        view, key = routed
        if at_bottom:
            self.call_after_refresh(self._scroll_trace_end)
            return
        head = view.head_widget(key)
        if head is not None:
            self.call_after_refresh(head.scroll_visible, animate=False)

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
        """Parent ToolCallRow for nested mount: active or durable subagent_*.

        Parent вне окна retention (light payload) — nested не монтируем.
        Нет call-события в ленте — якорь оставляем (live до записи сегментов).
        """
        if parent_outside_retention(self._task_events, parent_call_id):
            return None
        return self._active_calls.get(
            parent_call_id
        ) or self._subagent_parent_rows.get(parent_call_id)

    def _merge_sidecar_nested(self, task_id: str) -> None:
        """После rebuild parent-трейса: nested только для parent'ов из окна.

        read_subagent_trace читает файл целиком (разовый spike). В поля экрана
        полный список не кладём: фильтр и локальные actions умирают с вызовом.
        """
        if self._store is None or not task_id:
            return
        records = self._store.read_subagent_trace(task_id)
        if not records:
            return
        records = filter_sidecar_to_parents(
            records, subagent_parent_ids_from_events(self._task_events)
        )
        if not records:
            return
        actions = plan_sidecar_nested_actions(
            records, root=self._current_cwd()
        )
        apply_sidecar_nested_actions(
            actions,
            mount_nested_tool=partial(self._mount_nested_tool, live=False),
            finish_nested_tool=self._finish_nested_tool,
            mount_nested_prose=self._mount_nested_prose,
            mount_nested_summary=self._mount_nested_child_summary,
            mount_nested_user=self._mount_nested_user,
            mount_nested_todo=self._mount_nested_todo,
        )
        # Исход хода родителя — уровням, домонтированным после записи итога.
        self._block_views.settle_levels()

    def _nested_blocks(self, parent_call_id: str) -> Optional[ToolBlocksView]:
        """Исполнитель блоков уровня субагента (ключ — строка `Agent`);
        строки `Agent` в ленте нет — уровня нет."""
        parent = self._nested_parent_row(parent_call_id)
        if parent is None:
            return None
        view = self._block_views.get(parent_call_id)
        if view is None:
            model = ToolBlocks(
                self._call_timers,
                diff_preview=self._trace_inline_max_lines(),
                agent_window=True,
            )
            view = ToolBlocksView(
                _NestedBlockHost(self, parent_call_id),
                model,
                level=parent_call_id,
                depth=1,
            )
            view.set_owner(parent)
            self._block_views[parent_call_id] = view
        return view

    def _nested_mount_after(
        self, parent_call_id: str, widget, after=None, before=None
    ) -> None:
        """Смонтировать виджет уровня субагента: рядом с явным якорем
        (``after`` / ``before`` — виджеты блока) или в конец уровня — за
        его последним виджетом в ленте (§9.1). Внутри субагента пустых
        строк нет (`-in-agent`); виджет не из модели блоков — событие
        уровня: закрывает группу и сдвигает окно `Agent`."""
        view = self._nested_blocks(parent_call_id)
        if view is None:
            return
        parent = self._nested_parent_row(parent_call_id)
        trace = self.query_one(_SEL_TRACE, VerticalScroll)
        widget.add_class("-in-agent")
        try:
            if before is not None:
                trace.mount(widget, before=before)
            else:
                level = self._nested_widgets_by_parent.get(parent_call_id)
                anchor = after or nested_tail(
                    trace.children, level or (), parent
                )
                trace.mount(widget, after=anchor)
        except Exception:
            # Якоря нет в ленте — в конец, но над очередью; группы верхнего
            # уровня вложенный виджет не закрывает.
            self._trace_mount(widget, scroll=False, close_groups=False)
        self._nested_widgets_by_parent.setdefault(parent_call_id, []).append(
            widget
        )
        if not view.issued(widget):
            view.event_added(widget)

    def _mount_nested_tool(
        self,
        *,
        parent_call_id: str,
        tool_name: str,
        tool_call_id: str,
        args_summary: str,
        step: int = 0,
        max_steps: int = 0,
        live: bool = True,
        command: Optional[str] = None,
        **_kw,
    ) -> None:
        """Строка вложенного вызова — через модель уровня субагента;
        ``live`` — событие прогресса: вызов уже исполняется (у записи
        sidecar старта не было — время из реестра таймеров). ``command``
        — сырой текст команды Run-тула; сверх потолка события — из записи
        sidecar `call` (§4.0, §9.14 п. 7)."""
        view = self._nested_blocks(parent_call_id)
        if view is None:
            return
        # tool_name здесь уже wire-имя как есть: и subagent_progress.py
        # (live-polling, tools/subagents/progress.py::emit_progress), и
        # subagent_sidecar_mapper.py (payload.get("name")) отдают его сюда
        # МИНУЯ tool_id() — в отличие от верхнеуровневой ленты (trace_mapper.py).
        # Поэтому третьего wire/canonical-разделения (TraceEvent.wire_name,
        # Д11/R3) здесь не нужно. Если один из этих путей начнёт канонизировать
        # имя — этот сайт молча воспроизведёт тот же баг несогласованных имён.
        mcp_tools, mcp_servers = self._mcp_tool_info()
        row = build_call_row(
            call_id=tool_call_id or f"_nested_{tool_name}_{step}",
            wire_name=tool_name,
            args_summary=args_summary or "",
            level=1,  # tool_start = уже execute, не waiting
            mcp_tools=mcp_tools,
            mcp_servers=mcp_servers,
        )
        if tool_call_id:
            self._nested_tool_rows[tool_call_id] = row
        category, _ = tool_caption(
            tool_name, mcp_tools, mcp_servers, nested=True
        )
        is_run = block_kind(category) == BlockKind.RAN
        if command is None and is_run and tool_call_id:
            command = self._nested_sidecar_texts(tool_call_id)[0]
        lines = split_command(command, root=self._current_cwd())
        view.add_call(row, category, waiting=False, command=lines)
        if live:
            view.started(row)

    def _finish_nested_tool(
        self,
        *,
        parent_call_id: str,
        tool_call_id: str = "",
        ok: bool = True,
        error_brief: str | None = None,
        outcome: ResultOutcome | None = None,
        output: Optional[str] = None,
        **_kw,
    ) -> None:
        """Исход вложенного вызова — через модель уровня субагента, с
        правкой из журнала (у субагента свой `call_id`). ``output`` —
        сырой вывод: сводка и превью тем же маппером, что наверху; сверх
        потолка события — из записи sidecar у любого тула (§9.14 п. 5)."""
        row = (
            self._nested_tool_rows.get(tool_call_id) if tool_call_id else None
        )
        view = self._block_views.get(parent_call_id)
        if row is None or view is None:
            return
        is_run = view.model.kind_of(tool_call_id) == BlockKind.RAN
        if output is None:
            output = self._nested_sidecar_texts(tool_call_id)[1]
        ev = nested_result_event(
            tool_call_id,
            ok=ok,
            error_brief=error_brief,
            outcome=outcome,
            output=output,
        )
        edit, compact = self._block_edit(tool_call_id)
        ran = self._ran_result(ev) if is_run else None
        view.result(row, ev, edit, compact=compact, ran=ran)

    def _nested_sidecar_texts(
        self, call_id: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Команда и вывод вложенного вызова из sidecar открытого треда
        (записаны до события прогресса)."""
        if self._store is None or not self._active_task_id:
            return None, None
        records = self._store.read_subagent_trace(self._active_task_id)
        return nested_raw_texts(records or [], call_id)

    def _mount_nested_prose(
        self,
        *,
        parent_call_id: str,
        prose: str,
        ok: bool = True,
        error_brief: str | None = None,
        **_kw,
    ) -> None:
        """Read-only счёт-в-счёт с merge_nested_readonly._prose (trace_render.py)."""
        if self._nested_parent_row(parent_call_id) is None:
            return
        text = (prose or "").strip()
        if not text:
            return
        self._nested_mount_after(
            parent_call_id,
            build_nested_prose_row(text, ok=ok, error_brief=error_brief),
        )

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
        from gc_tui.services.todo_render import build_todo_block

        self._nested_mount_after(
            parent_call_id,
            build_todo_block(
                todo_items or [], subagent=True, indent=level_prefix(1)
            ),
        )

    def _mount_nested_user(
        self,
        *,
        parent_call_id: str,
        prose: str,
        images: tuple[Attachment, ...] = (),
        **_kw,
    ) -> None:
        """Mid-run user steer under active subagent_* (active-scope input) и ответ на
        вопрос субагента — с картинками ответа (визуальный контракт картинок §3).

        Read-only счёт-в-счёт с merge_nested_readonly._user (trace_render.py)."""
        from gc_tui.widgets.user_reply import NestedUserReply

        if self._nested_parent_row(parent_call_id) is None:
            return
        text = (prose or "").strip()
        if not text:
            return
        self._nested_mount_after(
            parent_call_id, NestedUserReply(text, images=images)
        )

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
        mcp_tools, mcp_servers = self._mcp_tool_info()
        row.set_live_status(
            format_parent_live_status(
                step=step,
                max_steps=max_steps,
                last_tool=last_tool,
                mcp_tools=mcp_tools,
                mcp_servers=mcp_servers,
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
        from gc_tui.state import AskHumanState
        from gc_tui.widgets.ask_human_banner import AskHumanBanner

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
            self._top_blocks().paused(parent_row)
        inp_row = self.query_one(_SEL_INPUT_ROW, Horizontal)
        self._mount_sticky_banner(banner, inp_row)
        inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
        self._sync_input_placeholder()
        inp.add_class("ask-human-mode")
        self._refresh_header()
        self._refresh_footer()
        self._update_terminal_title()

    def _hide_child_ask_human(
        self, *, parent_call_id: str = "", **_kw
    ) -> None:
        """Снять child ask-баннер после ответа; resume тикера корня."""
        parent_row = (
            self._active_calls.get(parent_call_id) if parent_call_id else None
        )
        if parent_row is not None:
            self._top_blocks().resumed(parent_row)
        self._unmount_ask_human_banner()

    def _diff_thread_identity(
        self, task_id: Optional[str] = None
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """(threads_dir, system_id, thread_id) для журнала правок: треда
        `task_id`, по умолчанию текущей сессии."""
        threads_dir = getattr(self.app, "_threads_dir", None)
        system_id = getattr(self.app, "_system", None)
        thread_id = (
            task_id
            or self._active_task_id
            or getattr(
                getattr(self.app, "tui_state", None), "active_task_id", None
            )
        )
        if not isinstance(threads_dir, str) or not threads_dir.strip():
            threads_dir = None
        if not isinstance(system_id, str) or not system_id.strip():
            system_id = None
        if not isinstance(thread_id, str) or not thread_id.strip():
            thread_id = None
        return threads_dir, system_id, thread_id

    def _edits_lookup_for_thread(self, task_id: Optional[str] = None):
        """EditsLookup треда `task_id` (по умолчанию активного); None —
        идентичность неполна.

        Один экземпляр на тред: внутри него кэш индекса журнала и кэш сводки
        хода, которые на каждом событии пересоздавать нельзя. `launch_root`
        нужен сводке — по нему находятся снапшоты и сами файлы на диске.

        Лимиты секции `agent_edits` входят в ключ кэша: без них правка порогов в
        настройках не доехала бы до расчёта до самой смены треда."""
        identity = self._diff_thread_identity(task_id)
        if not all(identity):
            return None
        from gc_tui.services.edits_view import diff_limits

        cfg = self.app.base_settings()
        launch_root = cfg.runtime.launch_root
        launch_root = str(launch_root) if launch_root else None
        max_source_bytes, hunk_context_lines = diff_limits(cfg)
        key = (*identity, launch_root, max_source_bytes, hunk_context_lines)
        if self._edits_lookup_key != key:
            from gc_tui.services.edits_lookup import lookup_for_settings

            self._edits_lookup = lookup_for_settings(
                *identity, cfg, launch_root=launch_root
            )
            self._edits_lookup_key = key
        return self._edits_lookup

    def _agent_edits_section(self) -> dict:
        """Секция `agent_edits` базовых настроек окна — словарём."""
        return self.app.base_settings().settings.agent_edits.model_dump()

    def _trace_inline_max_lines(self) -> int:
        """Порог свёртки инлайн-диффа — `agent_edits.trace_inline_max_lines`."""
        return edit_settings(self._agent_edits_section())[0]

    def _replay_expanded_turns(self) -> int:
        """Сколько последних ходов показывают дифф телом —
        `agent_edits.replay_expanded_turns`; `-1` — окно не режем
        (`edit_settings`)."""
        return edit_settings(self._agent_edits_section())[1]

    def _turn_summary_in_status(self) -> bool:
        """Показывать ли сводку правок в строке-итоге — `agent_edits.turn_summary_in_status`."""
        section = self._agent_edits_section()
        return bool(
            section.get(
                "turn_summary_in_status", DEFAULT_TURN_SUMMARY_IN_STATUS
            )
        )

    def _capture_edits_baseline(self) -> None:
        """Запомнить последний ход журнала правок ПЕРЕД началом нового.

        Ход без правок журнал не дописывает, поэтому одного `last_turn_id`
        на финише мало: без этой отметки сводка прошлого хода прилипла бы к
        следующему.

        Снимается РОВНО ОДИН раз за прогон. Отметку старта ставят дважды —
        из подготовки прогона и из страховочной проверки на восходящем фронте,
        а та приходит с тиком интерфейса. Успела первая правка записаться
        раньше тика — и вторая отметка переносила границу НА НЕЁ: ход, который
        правил файлы, оставался без сводки «изменено N файлов», а в meta треда
        не попадал last_indicator (пятый раунд ревью, находка 6). Проявлялось
        на быстрой модели и при занятом интерфейсе, то есть случайно.
        """
        if self._edits_baseline_taken:
            return
        lookup = self._edits_lookup_for_thread()
        self._edits_baseline_turn_id = (
            lookup.last_turn_id() if lookup is not None else None
        )
        self._edits_baseline_taken = True

    def _close_turn_edits(
        self, kind: str, task_id: Optional[str], *, on_screen: bool
    ) -> Optional[tuple]:
        """Конец прогона: сбросить состояние хода, вернуть сводку его правок.

        Агент мог создать скилл → TUI-кэш скиллов (/skill picker/inline,
        строки скиллов палитры) инвалидируется и тихо прогревается заново в
        фоне. Следующий прогон снимет свою границу журнала правок заново.
        Сводка правок — тихий правый сегмент строки итога; compact ходом не
        считается, правок в нём нет. Сводка — по журналу треда прогона
        `task_id`; строке — только если этот тред на экране."""
        self._skills_cache = None
        self._warm_skills_cache()
        self._edits_baseline_taken = False
        if kind == "compact":
            return None
        edits = self._finished_turn_summary(task_id)
        if on_screen:
            self._closed_turn_summary = edits
        return edits

    def _finished_turn_summary(
        self, task_id: Optional[str] = None
    ) -> Optional[tuple]:
        """(файлов, +, −) правок завершившегося хода; None — показывать нечего.

        None — сегмент выключен ключом, журнал недоступен или ход не дописал в
        журнал ни одной записи (последний ход в нём — тот же, что на старте).

        None и при отказе чтения: база хода для счётчиков читается из индекса
        снимков под замком, и занятый другим писателем замок отвечает таймаутом —
        прямо в колбэке таймера (`_poll_active`), где исключение завершило бы
        приложение. Сводка — украшение строки-итога: не прочиталась — её нет."""
        if not self._turn_summary_in_status():
            return None
        lookup = self._edits_lookup_for_thread(task_id)
        if lookup is None:
            return None
        turn_id = lookup.last_turn_id()
        if not turn_id or turn_id == self._edits_baseline_turn_id:
            return None
        try:
            files, add, del_ = lookup.turn_summary(turn_id)
        except OSError:  # TimeoutError замка — подкласс OSError
            _log.warning("сводка правок хода не прочитана", exc_info=True)
            return None
        return (files, add, del_) if files > 0 else None

    def _restored_turn_summary(self, edits) -> Optional[tuple]:
        """Сводка правок для строки-итога, восстановленной из meta.

        Цифры берём ТОЛЬКО из записи того же хода (`last_indicator.edits`):
        журнал по последнему ходу дал бы правки предыдущего хода, если ход
        перед закрытием сессии ничего не правил. Записи нет (сессия старше
        фичи) — сегмента нет."""
        if not self._turn_summary_in_status():
            return None
        if not isinstance(edits, (list, tuple)) or len(edits) != 3:
            return None
        try:
            files, add, del_ = (int(v) for v in edits)
        except (TypeError, ValueError):
            return None
        return (files, add, del_) if files > 0 else None

    def _on_ask_human_event(self, ev, *, live: bool = False) -> None:
        from gc_tui.widgets.ask_human_banner import AskHumanBanner

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
        from gc_tui.services.trace_render import build_answered_question_widget

        self._ask_human_question_row = build_answered_question_widget(
            ev.summary,
            event_no=ev.no,
            extra_classes="ask-human-question hidden",
            call_id=ev.call_id,
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
        from gc_tui.state import AskHumanState

        self.app.tui_state.ask_human_state = AskHumanState(
            call_id=ev.call_id or "",
            question=ev.summary,
        )
        # mount banner before #input-row; потолок высоты — после layout
        # (_cap_sticky_banner), иначе длинный вопрос выдавливает input/footer.
        inp_row = self.query_one(_SEL_INPUT_ROW, Horizontal)
        self._mount_sticky_banner(banner, inp_row)
        # switch input to ask-human mode
        inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
        self._sync_input_placeholder()
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
        self._sync_indicator_bar()
        self._ask_human_call_id = None
        self.app.tui_state.ask_human_state = None
        inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
        self._sync_input_placeholder()
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
        decision_key: str | None = None,
    ) -> None:
        """Смонтировать ConfirmBanner и переключить в режим одиночной клавиши.

        Вызывается на UI-потоке (напр. через App.call_from_thread из T3).
        Скрывает #input-row, убирает фокус с TextArea — «y»/«n» доходят до
        Screen.on_key и не поглощаются TextArea. fields — структурированные
        детали решения (Tool/Path/Command/Reason/Risk) из confirm-порта.
        via — метка субагента для child HITL (опционально)."""
        from gc_tui.widgets.confirm_banner import ConfirmBanner

        if self._confirm_active:
            self._hide_confirm_banner()
        banner = ConfirmBanner(
            banner_title=title,
            question=question,
            fields=fields,
            via=via,
            decision_key=decision_key,
            permission_mode=self.app.effective_permission_mode(),
        )
        self._confirm_banner = banner
        self._confirm_active = True
        inp_row = self.query_one(_SEL_INPUT_ROW, Horizontal)
        self._mount_sticky_banner(banner, inp_row)
        inp_row.add_class("hidden")
        # Инпут на время confirm: СИНХРОННО disabled (иначе «y» утекает в TextArea).
        # Фокус — на сам баннер, не set_focus(None): при None следующий Tab/клик
        # по running ToolCallRow перехватывает Enter и открывает tool detail
        # вместо _answer_confirm (Key идёт focused→bubble, Screen.on_key позже).
        try:
            self.query_one(_SEL_CMDINPUT, CommandTextArea).disabled = True
        except Exception:
            pass
        self.set_focus(banner)
        if self._stick_bottom:
            self._scroll_trace_end()

    def _hide_confirm_banner(self) -> None:
        """Снять ConfirmBanner и восстановить поле ввода."""
        self._confirm_active = False
        self._return_focus_to_input()
        if self._confirm_banner is not None:
            try:
                self._confirm_banner.remove()
            except Exception:
                pass
            self._confirm_banner = None
        self._sync_indicator_bar()
        self._settle_trace_after_banner()

    def _return_focus_to_input(self) -> None:
        """Вернуть поле ввода и фокус в него — ДО снятия баннера. Фокус стоит
        на баннере: удалённый первым, он отдал бы фокус Textual, а тот перевёл
        бы его на фокусируемую строку ленты и прокрутил ленту к ней — человек,
        ушедший читать вверх, улетал бы в конец. Поэтому `set_focus` экрана
        (сразу), а не `focus()` (отложенный: к удалению баннера фокус был бы
        ещё на нём)."""
        try:
            self.query_one(_SEL_INPUT_ROW, Horizontal).remove_class("hidden")
        except Exception:
            pass
        try:
            inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
            inp.disabled = (
                False  # снять disable ДО focus (фокус не идёт на disabled)
            )
            self.set_focus(inp)
        except Exception:
            pass

    def _settle_trace_after_banner(self) -> None:
        """Баннер снят: место под ним отдаётся ленте, и её низ сдвигается на
        высоту баннера. Опрос принял бы сдвиг за уход человека вверх и снял
        бы липкость — лента, прижатая к низу до снятия, туда и возвращается."""
        sticky = self._stick_bottom

        def _settle() -> None:
            self._sync_trace_max_height()
            if sticky:
                self._stick_bottom = True
                self._scroll_trace_end()

        self.call_after_refresh(_settle)

    # ───────────────────────────── одобрение плана ─────────────────────────────

    def _show_plan_banner(self, *, resumed: bool = False) -> None:
        """Смонтировать баннер плана — по образцу confirm-баннера: поле ввода
        скрыто и выключено, фокус на баннере. `resumed` — план уже утверждён,
        вопрос о продолжении."""
        from gc_tui.widgets.plan_banner import PlanApprovalBanner

        if self._plan_banner is not None:
            return
        banner = PlanApprovalBanner(resumed=resumed)
        self._plan_banner = banner
        inp_row = self.query_one(_SEL_INPUT_ROW, Horizontal)
        self._mount_sticky_banner(banner, inp_row)
        inp_row.add_class("hidden")
        try:
            self.query_one(_SEL_CMDINPUT, CommandTextArea).disabled = True
        except Exception:
            pass
        self.set_focus(banner)
        if self._stick_bottom:
            self._scroll_trace_end()

    def _hide_plan_banner(self) -> None:
        """Снять баннер плана и вернуть поле ввода — единая точка для всех
        путей снятия (выбор варианта, Esc, смена сессии)."""
        if self._plan_banner is None:
            return
        self._return_focus_to_input()
        try:
            self._plan_banner.remove()
        except Exception:
            pass
        self._plan_banner = None
        self._sync_indicator_bar()
        try:
            self._sync_input_placeholder()
        except Exception:
            pass
        self._settle_trace_after_banner()

    def _session_plan(self) -> Optional[dict]:
        """План открытой сессии из её меты — то, что загрузит следующий прогон
        (во время прогона мета тоже свежая: состояние сливается в неё на каждом
        батче). Модель в этом не участвует."""
        if self._store is None or self._active_task_id is None:
            return None
        shared = self._store.get_task_meta(self._active_task_id).get("shared")
        plan = (shared or {}).get("plan")
        return plan if isinstance(plan, dict) and plan.get("text") else None

    def _plan_panels(self):
        try:
            return list(self.query(PlanPanel))
        except Exception:
            return []

    def _mark_plan_accepted(self, accepted: bool, plan_text: str) -> None:
        """Все рамки этого плана в ленте (исходная и копии): серые, пока план
        ждёт решения; цвета режима plan — когда принят и идёт реализация."""
        for panel in self._plan_panels():
            if panel.shows(plan_text):
                panel.set_accepted(accepted)

    def _sync_plan_panel_with_session(self) -> None:
        """Привести рамки к сессии: утверждённой остаётся только рамка плана,
        принятого в сессии; прежние (заменённые) планы — серые. Зовётся там,
        где лента перерисована или статус мог смениться."""
        plan = self._session_plan()
        for panel in self._plan_panels():
            panel.set_accepted(plan_is_accepted(plan, panel.plan_text))

    def _ensure_plan_panel_last(
        self, plan: dict, *, above_fresh_line: bool = False
    ) -> None:
        """Рамка плана сессии стоит последней в ленте (над очередью): если
        нет — показать копию. Копия — локальный рендер, в историю не
        пишется.

        ``above_fresh_line`` — рамка по итогу хода «План готов»: её место
        над свежей строкой этого хода (S10 §3.5)."""
        from gc_tui.services.trace_render import build_block_widgets

        text = str(plan.get("text") or "")
        from gc_tui.widgets.user_reply import is_queued

        trace = self.query_one(_SEL_TRACE, VerticalScroll)
        # Последний — без очереди: она прижата к низу и рамку не хоронит.
        children = [w for w in trace.children if not is_queued(w)]
        anchor = None
        if (
            above_fresh_line
            and children
            and children[-1] is self._fresh_turn_line()
        ):
            anchor = children.pop()
        last = children[-1] if children else None
        if isinstance(last, PlanPanel) and last.shows(text):
            return
        # event_no=0: строки копии не привязаны к событию истории — клик по
        # ним не должен открывать чужое событие.
        panel = PlanPanel(
            *build_block_widgets(text, prose_bullet=False, event_no=0),
            plan_text=text,
        )
        live = self._stream_answer_row or self._stream_block
        if live is not None and live.parent is trace:
            # Идёт потоковый ответ: копия встаёт ПЕРЕД ним, иначе готовый
            # ответ потом перепрыгнул бы через план.
            self._note_top_event(panel)
            trace.mount(panel, before=live)
        elif anchor is not None:
            self._note_top_event(panel)
            trace.mount(panel, before=anchor)
        else:
            self._trace_mount(panel)
        panel.set_accepted(plan_is_accepted(plan, text))

    def _present_session_plan(self) -> None:
        """Ход закончился планом: рамка в ленте (её может не быть, если агент
        предъявил сохранённый план заново) и выбор по его статусу."""
        plan = self._session_plan()
        if plan is None:
            return
        self._ensure_plan_panel_last(plan, above_fresh_line=True)
        self._sync_plan_panel_with_session()
        self._show_plan_banner(resumed=plan_is_accepted(plan))

    def _plan_file_row(self) -> None:
        """Под планом, открытым командой: где лежит его файл и как убрать план.
        Две отдельные строки ленты — между ними воздух, как между любыми
        служебными строками: путь и подсказка про команду — разные сведения."""
        from gc_core.thread_store import plan_file_path

        threads_dir = self.app._threads_dir or ""
        if threads_dir and self._active_task_id:
            path = plan_file_path(
                threads_dir, self.app._system, self._active_task_id
            )
            if os.path.exists(path):
                home = os.path.expanduser("~")
                shown = (
                    "~" + path[len(home) :] if path.startswith(home) else path
                )
                self._mount_trace_row(
                    prefix="· ",
                    content=f"план сохранён: {shown}",
                    kind_class="line-meta",
                )
        self._mount_trace_row(
            prefix="· ", content=_PLAN_CLEAR_HINT, kind_class="line-meta"
        )

    def _cmd_plan(self, arg: str = "") -> None:
        """/plan — вернуться к плану сессии: показать его рамкой в конце ленты
        и, если исполнитель свободен, дать выбор. Показывает и утверждает
        программа, а не модель: та пересказала бы план по памяти.

        Режим не трогается, когда план есть: его меняет выбор в баннере.
        Плана нет — команда включает plan, и при занятом исполнителе.
        """
        arg = (arg or "").strip()
        if arg == "clear":
            self._cmd_plan_clear()
            return
        if arg:
            self._mount_trace_row(
                prefix="[!] ",
                content=f"/plan: аргумент {arg!r} не поддерживается",
                kind_class="line-error",
            )
            return
        if self._plan_banner is not None:
            return
        runner = self.app.active_runner
        busy = runner is not None and runner.is_running()
        plan = self._session_plan()
        if plan is None:
            already = self.app.effective_permission_mode() == Mode.PLAN.value
            if not already:
                # Как Shift+Tab — и посреди хода: смену объявят модели.
                self.app.set_permission_mode(Mode.PLAN.value)
                self.on_permission_mode_changed(Mode.PLAN.value)
            if busy:
                # Следов в ленте нет: ответ — подсказка смены режима в футере.
                if already:
                    self.app.notify(_PLAN_CMD_NO_PLAN_BUSY)
                return
            self._mount_trace_row(
                prefix="· ",
                content=_PLAN_CMD_NO_PLAN if already else _PLAN_CMD_ENTERED,
                kind_class="line-meta",
            )
            return
        self._ensure_plan_panel_last(plan)
        self._plan_file_row()
        if not busy:
            # Пока исполнитель занят, выбора нет: продолжение не стартует.
            self._show_plan_banner(resumed=plan_is_accepted(plan))

    def _cmd_plan_clear(self) -> None:
        """/plan clear — убрать план из сессии: он перестаёт показываться
        командой и ехать в сводку при сжатии истории; модели об этом сообщит
        следующий прогон. Рамки в ленте остаются — это история."""
        runner = self.app.active_runner
        if runner is not None and runner.is_running():
            self.app.notify(
                "агент работает — уберите план после завершения хода"
            )
            return
        if self._plan_banner is not None:
            return
        removed = False
        clear = getattr(self._store, "clear_plan", None)
        if callable(clear) and self._active_task_id is not None:
            removed = bool(clear(self._active_task_id))
        if runner is not None and hasattr(runner, "clear_plan"):
            runner.clear_plan(self._active_task_id)
        self._plan_rework_pending = False
        self._sync_plan_panel_with_session()
        self._sync_input_placeholder()
        self._mount_trace_row(
            prefix="· ",
            content="план убран из сессии" if removed else "плана и так нет",
            kind_class="line-meta",
        )

    def _sync_input_placeholder(self) -> None:
        """Подсказка в пустом поле ввода — одна точка на все состояния:
        вопрос агента > замечания к плану > план ждёт решения > обычная (с
        подсказкой F1, пока она не погашена)."""
        try:
            inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
        except Exception:
            return
        if self.app.tui_state.ask_human_state is not None:
            inp.placeholder = _PLACEHOLDER_ASK_HUMAN
        elif self._plan_rework_pending:
            inp.placeholder = _PLACEHOLDER_PLAN_REWORK
        elif (
            self._plan_banner is None
            and self.app.effective_permission_mode() == Mode.PLAN.value
            and plan_awaits_decision(self._session_plan())
        ):
            inp.placeholder = _PLACEHOLDER_PLAN_WAITS
        elif self._help_hint_spent:
            inp.placeholder = _PLACEHOLDER_READY
        else:
            inp.placeholder = _PLACEHOLDER_READY_HELP

    def spend_help_hint(self) -> None:
        """Погасить подсказку F1 в поле ввода до конца запуска: человек
        отправил сообщение, открыл справку или палитру, начал новую сессию."""
        if self._help_hint_spent:
            return
        self._help_hint_spent = True
        self._sync_input_placeholder()

    def _apply_plan_choice(self, choice: str) -> None:
        """Выбран вариант баннера: режим исполнения либо «Обсудить и доработать»."""
        from gc_tui.widgets.plan_banner import PLAN_CHOICE_REWORK

        if choice == PLAN_CHOICE_REWORK:
            self._rework_plan()
        else:
            self._accept_plan(choice)

    def _rework_plan(self) -> None:
        """«Обсудить и доработать»: вернуть поле ввода и режим plan.

        Статус плана здесь не меняется — взводится только намерение: в
        доработку план уйдёт, когда человек реально отправит сообщение. Режим
        переключается из любого состояния: иначе агент начнёт править файлы
        вместо того, чтобы предложить исправленный план.
        """
        plan = self._session_plan()
        self._hide_plan_banner()
        self.app.get_runner().arm_plan_rework(
            self.app.tui_state.active_task_id
        )
        self._plan_rework_pending = True
        if plan is not None:
            # принятый план вернули в обсуждение
            self._mark_plan_accepted(False, plan["text"])
        self.app.set_permission_mode(Mode.PLAN.value)
        self.on_permission_mode_changed(Mode.PLAN.value)
        self._sync_input_placeholder()

    def _accept_plan(self, mode: str) -> None:
        """План утверждён: режим, статус и автоматическое продолжение."""
        # Признак «утверждён повторно» живёт на баннере — читается ДО снятия.
        resumed = bool(getattr(self._plan_banner, "resumed", False))
        plan = self._session_plan()
        runner = self.app.get_runner()
        if runner.is_running():
            self.app.notify(
                "агент работает — утвердите план после завершения хода"
            )
            return
        self._plan_rework_pending = False
        self._hide_plan_banner()
        # Прогон без реплики человека: откатывать по Esc нечего.
        self._last_user_message_id = None
        if plan is not None:
            self._mark_plan_accepted(True, plan["text"])
        self.app.set_permission_mode(mode)
        self._refresh_footer()
        thread_id = self.app.tui_state.active_task_id
        # Тред уезжает вместе с решением: оно не должно примениться к прогону
        # другой сессии, если продолжение не стартовало.
        runner.accept_plan(mode, thread_id)
        # Решение человека — галочкой цвета режима plan (тот же, что у рамки
        # утверждённого плана), не зелёной: зелёная «✓» в ленте — успешный
        # вызов инструмента.
        self._mount_trace_row(
            prefix="✓ ",
            # Повторное решение по уже утверждённому плану — не новое
            # утверждение: строка говорит, что выполнение продолжено.
            content=(
                f"выполнение плана продолжено → {mode_label(mode)}"
                if resumed
                else f"план утверждён → {mode_label(mode)}"
            ),
            kind_class="line-meta line-plan-accepted",
        )
        self._prepare_for_new_run()
        self._reset_thinking_timer()
        (query,) = accepted_plan_messages(mode, resumed=resumed)
        runner.start(
            query=query,
            thread_id=thread_id,
            system=self.app._system,
        )

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
        ah_idx = self._last_ask_human_index(self._task_events)
        if ah_idx is None:
            return False
        meaningful = _DIALOG_CONTINUATION_KINDS | {"summary"}
        return any(
            ev.kind in meaningful for ev in self._task_events[ah_idx + 1 :]
        )

    def _last_ask_human_index(self, events: list) -> int | None:
        """Индекс последнего события смонтированного вопроса ask_human."""
        cid = self._ask_human_call_id
        ah_idx = None
        for i, ev in enumerate(events):
            if ev.kind == "ask_human" and (not cid or ev.call_id == cid):
                ah_idx = i
        return ah_idx

    def _reconcile_ask_human_after_render(
        self, *, allow_remnant: bool
    ) -> None:
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
        question = self._ask_human_question_row
        self._unmount_ask_human_banner()
        if not allow_remnant:
            return
        from gc_tui.services.trace_render import (
            QUESTION_INTERRUPTED,
            note_question,
        )

        if question is not None:
            # Следствие — строкой `└` в рамке вопроса «? …» (её монтирует
            # _on_ask_human_event), не отдельной строкой ленты.
            note_question(question, QUESTION_INTERRUPTED)
            return
        # Строки вопроса в ленте нет (баннер ребёнка) — отдельной строкой.
        self._mount_trace_row(
            prefix="", content=QUESTION_INTERRUPTED, kind_class="line-meta"
        )

    def _mount_user_reply(
        self,
        text: str,
        *,
        event_no: int = 0,
        state: str = "normal",
        client_id: str | None = None,
        images: tuple[Attachment, ...] = (),
        skill: str | None = None,
        skill_error: bool = False,
        message_id: str | None = None,
    ):
        """Смонтировать реплику; ``skill`` — id скилла (имя первым словом).

        Свежая отправка (`pending` / `queued`) без явного скилла забирает итог
        доставки `_pending_skill_marker` (спека P3 §2.3)."""
        from gc_tui.widgets.user_reply import UserReplyContainer

        fresh = state in ("pending", "queued")
        if fresh and skill is None:
            marker, self._pending_skill_marker = (
                self._pending_skill_marker,
                None,
            )
            if marker is not None:
                skill, skill_error = marker[0], not marker[1]
        widget = UserReplyContainer(
            text,
            event_no=event_no,
            state=state,
            images=images,
            skill=skill or None,
            skill_error=skill_error,
            message_id=message_id,
            client_id=client_id,
        )
        # pending/queued = свежая отправка юзера → форсим скролл вниз
        # (re-engage); normal/failed = воспроизведение из истории → обычное
        # прилипание.
        self._trace_mount(widget, force_scroll=fresh)
        if fresh and client_id is not None:
            self._record_pending_widget(client_id, widget)
        return widget

    def _drain_trace_notes(self) -> None:
        """Пометки ленты из очереди — строкой `└ ! …` под владельцем
        (спека P3 §10, ответ 4); владельца нет на экране — строка `!` `WARN`.

        Ядро кормит очередь из рабочего потока через on_trace_note; строка
        монтируется здесь, на UI-потоке. Только вживую и только своего треда: в тред
        пометка не пишется, при открытии сессии заново не рисуется, пометка хода
        другой сессии сюда не попадает."""
        from gc_tui.services.trace_render import note_consequence

        sink = getattr(self.app, "_trace_note_sink", None)
        if sink is None:
            return
        tid = self._active_task_id
        waiting = [(n, t) for t_id, n, t in self._waiting_notes if t_id == tid]
        fresh = [(n, _NOTE_OWNER_WAIT_TICKS) for n in sink.drain(tid)]
        self._waiting_notes = []
        for note, ticks in waiting + fresh:
            owner = self._trace_note_owner(note)
            if owner is not None:
                owner.add_consequence(note_consequence(note.text))
            elif note.owner == "call" and ticks > 0:
                # Строка вызова ещё не в ленте — ждём её несколько тиков.
                self._waiting_notes.append((tid, note, ticks - 1))
            else:
                self._mount_standalone(note.text, "warn")

    def _trace_note_owner(self, note):
        """Владелец пометки в ленте: строка вызова `call_id` (верхняя или
        вложенная) или реплика — последняя с картинками, иначе последняя."""
        from gc_tui.widgets.tool_call_row import ToolCallRow
        from gc_tui.widgets.user_reply import UserReplyContainer

        trace = self.query_one(_SEL_TRACE, VerticalScroll)
        if note.owner == "call":
            rows = trace.query(ToolCallRow)
            return next((r for r in rows if r.call_id == note.call_id), None)
        replies = list(trace.query(UserReplyContainer))
        with_images = [r for r in replies if r.has_images]
        return (with_images or replies or [None])[-1]

    def _mount_standalone(
        self,
        text: str,
        severity: Literal["error", "warn"],
        *,
        event_no: int = 0,
    ) -> None:
        """Строка без владельца: `!` в колонке `●` (спека P3 §10, ответ 5)."""
        from gc_tui.services.trace_render import build_standalone_row

        self._trace_mount(
            build_standalone_row(text, severity=severity, event_no=event_no)
        )

    _PENDING_CAP = 20

    def _track_pending(
        self,
        display_text: str,
        wire_content: str,
        images: tuple[Attachment, ...] = (),
        queued: Optional[dict] = None,
    ) -> str:
        """Записать pending-реплику в meta.tui + положить в `_pending_user_widgets`.

        ``queued`` — поля реплики в очереди (`message_id`, `skill`): по ним пересборка ленты во время хода монтирует её снова
        в очереди (`_mount_orphaned_reply`).

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
            "ts_utc_iso": datetime.now(UTC).isoformat(),
            "status": "pending",
        }
        # Картинки реплики — чтобы упавшая после рестарта реплика перемонтировалась
        # с подстроками (визуальный контракт §3); ключ только при картинках.
        if images:
            record["attachments"] = [att.to_dict() for att in images]
        if queued:
            record.update(queued)
        existing = (self._store.get_task_meta(tid).get("tui") or {}).get(
            "pending_user_messages", []
        )
        new_list = (existing + [record])[-self._PENDING_CAP :]
        self._write_tui_meta(tid, {"pending_user_messages": new_list})
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
            self._write_tui_meta(tid, {"pending_user_messages": new_list})

    def _mark_pending_failed(self) -> None:
        """При фейле run'а: все текущие pending-виджеты → state=failed,
        записи в meta.tui.pending_user_messages → status=failed.
        Очищает self._pending_user_widgets (виджеты остаются на экране как failed)."""
        if not self._pending_user_widgets:
            return
        tid = self.app.tui_state.active_task_id
        for _cid, widget in self._pending_user_widgets.items():
            widget.set_state("failed")
        if tid is not None and self._store is not None:
            records = (self._store.get_task_meta(tid).get("tui") or {}).get(
                "pending_user_messages", []
            )
            failed_cids = set(self._pending_user_widgets.keys())
            new_list = [
                {**r, "status": "failed"}
                if r.get("client_id") in failed_cids
                else r
                for r in records
            ]
            self._write_tui_meta(tid, {"pending_user_messages": new_list})
        self._pending_user_widgets.clear()
        self._held_client_ids.clear()
        self._refresh_queue_hint()

    def _begin_auto_created_task(self) -> str:
        """Автосоздание треда под первое сообщение: id, store, наследование
        сессионных настроек режимов. Единая точка для всех входов
        (свободный текст, inline /skill, /init)."""
        tid = uuid.uuid4().hex
        self.app.tui_state.active_task_id = tid
        self._active_task_id = tid
        # Номера картинок, выданные до появления треда, — уже его номера.
        if None in self._image_seq_issued:
            self._image_seq_issued[tid] = self._image_seq_issued.pop(None)
        # Открываем store сейчас, чтобы _track_pending мог писать meta.
        self._store = FileTaskStore(
            threads_dir=self.app._threads_dir or "",
            system=self.app._system,
        )
        # /new и автосоздание: новый тред наследует то, с чем работаем сейчас.
        self.app.persist_session_overrides()
        # Тред открыт до старта хода: перестройка ленты не застанет реплику,
        # уже записанную быстрым исполнителем, без её отправленной копии.
        self.attach_active_task(tid)
        return tid

    def _prepare_for_new_run(self) -> None:
        """Подготовить TUI к новому запуску runner.start:
        сбросить error-флаг, очистить last_runner_error,
        сбросить meta.tui.status → running (B1: иначе после прошлого completed
        бейдж/список сессий врут, пока живой runner не перекрывает live).

        _mcp_status_row НЕ обнуляем: колонка — часть бренд-карточки (#brand-mcp),
        переживает пересборку (_clear_trace); ссылка остаётся валидной."""
        # Сначала конец прошлого хода: кончился между тиками — его исход
        # пишется с его флагами, до сброса.
        self._settle_ended_run()
        self._reset_run_flags()
        self._stamp_tui_running()
        self._run_pending = True
        self._sync_indicator_bar()  # бар — вместе с репликой
        # Строка итога — уже нового хода: мигание прошлого стопа её не
        # перекрывает, даже пока поток того хода дорабатывает. Сам ход
        # начнётся после того потока: тот дописывает запись итога стопа.
        runner = self.app.active_runner
        if runner is not None and self._stopping_alive():
            runner.follow(self._stopping_runner)
        self._stopping_runner = None

    def _settle_ended_run(self) -> None:
        """Прогон кончился — исход в его тред (`_on_run_finished`), ровно раз.

        Прогон — тот, что тик застал бегущим, иначе запущенный отсюда и ещё
        не увиденный. Кончился — его раннер не бежит или снят с app (стоп,
        пересборка, новый раннер)."""
        if not (self._was_running or self._run_pending):
            return
        current = getattr(self.app, "active_runner", None)
        # Стоп раньше первого тика, увидевшего прогон: прогон — тот, что
        # дорабатывает, а не сменный раннер (S10 §4.4).
        runner = self._watched_runner or self._stopping_runner or current
        if (
            runner is current
            and runner is not None
            and runner.is_running()
            and not self._run_replaced(runner)
            # Ошибка раннера — конец прогона, даже пока его поток жив:
            # строка «✕» встаёт в этом тике (S10 §3.7).
            and not self._active_run_error()
        ):
            return
        kind = getattr(runner, "_thread_kind", "run") if runner else "run"
        seq = self._watched_run_seq
        if seq is None:
            seq = getattr(runner, "run_seq", None)
        try:
            self._on_run_finished(runner, kind)
        except Exception:
            # Сбой записи исхода не повторяем на каждом тике: ход закрыт,
            # причина — в лог.
            _log.exception("run_settle_failed kind=%s", kind)
        finally:
            # Прогон зафиксирован: его пару фронты больше не видят, а
            # снимок меты до него откату уже не вернуть.
            if runner is not None:
                self._settled_run = (runner, seq)
            self._pre_run_tui_meta = None
            self._was_running = False
            self._run_pending = False
            self._watched_runner = None
            self._watched_run_seq = None
            self._sync_indicator_bar()

    def _run_replaced(self, runner) -> bool:
        """Раннер бежит, но уже другим прогоном, чем застал тик: хвост из
        очереди стартовал между тиками."""
        seen = self._watched_run_seq
        return seen is not None and getattr(runner, "run_seq", seen) != seen

    def _reset_run_flags(self) -> None:
        """Сбросить error- и stop-флаги прошлого прогона (app-уровня)."""
        self._error_shown = False
        self._mcp_status_done = False
        self.app.last_runner_error = None
        self.app.last_terminal_outcome = None
        self.app.last_stop_requested = False

    def _stamp_tui_running(self, task_id: str | None = None) -> None:
        """meta.tui.status=running + сброс last_indicator (новый ход, не итог
        прошлого) + отметка журнала правок для сводки этого хода.

        `task_id` — тред прогона; не задан — открытый (старт из него)."""
        self._capture_edits_baseline()
        tid = task_id or self._active_task_id
        if self._store is None or tid is None:
            return
        if tid == self._active_task_id:
            self._snapshot_pre_run_tui(tid)
        self._write_tui_meta(
            tid, {"status": "running", "last_indicator": None}
        )
        if tid == self._active_task_id:
            # Начался новый ход: запись итога прошлого — уже не его строка,
            # она оседает в ленте до реплики нового.
            self._outcome_superseded = True
            self._closed_turn_summary = None
            self._settle_turn_line()

    def _snapshot_pre_run_tui(self, tid: str) -> None:
        """Снимок `tui` треда до «running» нового хода — его вернёт откат.
        Уже «running» — ход отмечен (отправкой; восходящий фронт — повтор):
        снимок до него не переписывается."""
        tui = self._store.get_task_meta(tid).get("tui") or {}
        if tui.get("status") != "running":
            self._pre_run_tui_meta = (
                tid,
                {k: tui.get(k) for k in ("status", "last_indicator")},
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
        from gc_tui.services.mcp_progress import (
            format_mcp_progress,
            format_mcp_settle,
        )

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
            self._refresh_notices_card(
                mcp_line=mcp_line
            )  # запоминает mcp_line для перерисовок
        except Exception:
            pass
        self._maybe_toast_warnings()  # notices финальны (mcp_status осел) → warning-тосты

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

    def _mcp_tool_info(self) -> tuple[dict, tuple]:
        """Подписи MCP из снимка рантайма: (wire→McpToolInfo, серверы).

        Пересчёт — только при смене источника (`_mcp_snapshot_key`): строки
        вызова одной перерисовки не перечитывают meta треда."""
        from gc_core.constants.tool_display import (
            mcp_servers_of,
            mcp_tools_from_snapshot,
        )

        key = self._mcp_snapshot_key()
        cache = self._mcp_info_cache
        if cache is None or cache[0] != key:
            snap = self._mcp_screen_snapshot()
            cache = (
                key,
                mcp_tools_from_snapshot(snap),
                mcp_servers_of(snap),
            )
            self._mcp_info_cache = cache
        return cache[1], cache[2]

    def _mcp_snapshot_key(self) -> tuple:
        """Ключ источника снимка MCP — без чтения самого снимка.

        Boot-снимок — сам объект (в ключе он и жив, id не переиспользуется);
        иначе — тред и метка изменения его meta."""
        snap = getattr(self.app, "_mcp_snapshot", None)
        if snap is not None:
            return ("app", id(snap), snap)
        tid = self._active_task_id
        if self._store is None or tid is None:
            return ("none",)
        return ("meta", tid, self._store.meta_mtime(tid))

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
        return textual_compat.is_selecting(self)

    def _check_auto_scroll(
        self, select_widget, mouse_coordinate, delta_y
    ) -> None:
        """Автопрокрутка выделения Textual плюс протяжка ниже ленты (спека
        P3 §2.7): над накладками, полем ввода и футером лента едет вниз,
        конец выделения остаётся в ленте (`textual_compat`)."""
        textual_compat.check_auto_scroll(
            self,
            self.query_one_optional(_SEL_TRACE),
            select_widget,
            mouse_coordinate,
            delta_y,
        )

    def on_mouse_move(self, _event: events.MouseMove) -> None:
        """F5: движение мыши при выделении — активность, сторож ждёт."""
        if self._ui_frozen_for_select():
            self._select_freeze_since = time.monotonic()

    def _select_auto_scrolling(self) -> bool:
        """Идёт автопрокрутка выделения и ленте ещё есть куда ехать."""
        if not textual_compat.auto_scroll_running(self):
            return False
        try:
            trace = self.query_one(_SEL_TRACE, VerticalScroll)
        except NoMatches:
            return False
        return 0 < trace.scroll_y < trace.max_scroll_y

    def _select_watchdog(self) -> bool:
        """F5: выделение без активности 5 с снимается (runner не стопаем).
        Активность — движение мыши и тик идущей автопрокрутки. `True` —
        выделение живо, UI заморожен."""
        if not self._ui_frozen_for_select():
            self._select_freeze_since = None
            return False
        now = time.monotonic()
        if self._select_freeze_since is None or self._select_auto_scrolling():
            self._select_freeze_since = now
        if now - self._select_freeze_since <= 5.0:
            return True
        try:
            self.clear_selection()
        except Exception:
            pass
        self._select_freeze_since = None
        textual_compat.stop_selecting(self)
        return False

    def _poll_active(self) -> None:
        self._in_poll_tick = True
        try:
            self._poll_tick()
        finally:
            self._in_poll_tick = False
            self._tick_meta_cache = None

    def _apply_human_wait(self, human_wait: bool) -> None:
        """Пауза running-строк на человеко-ожидании и тик их elapsed.

        Вложенные строки субагента тоже: тул субагента может стоять на
        подтверждении, пока корень ждёт. Завершённые строки — no-op.
        """
        view = self._top_blocks()
        for row in self._active_calls.values():
            view.paused(row) if human_wait else view.resumed(row)
            row.refresh_running_elapsed()
        self._pause_nested_rows(human_wait)

    def _nested_rows(self):
        """Вложенные строки вызовов с исполнителем своего уровня."""
        for row in self._nested_tool_rows.values():
            level = row.block_key[0] if row.block_key else None
            view = self._block_views.get(level) if level else None
            if view is not None:
                yield view, row

    def _pause_nested_rows(self, human_wait: bool) -> None:
        """Вложенные строки субагента — через модель своего уровня."""
        for view, row in self._nested_rows():
            view.paused(row) if human_wait else view.resumed(row)
            row.refresh_running_elapsed()

    def _interrupt_nested_rows(self) -> None:
        """Инвариант «прервано» для вложенных строк субагента — через
        модель своего уровня."""
        for view, row in list(self._nested_rows()):
            view.interrupted(row, started=not row.waiting)

    def _runner_is_for_active_thread(self) -> bool:
        """Раннер работал (работает) в открытом треде. Раннер и его
        `last_runner_error` общие на приложение: решения о том, что рисовать
        в открытом треде, берутся только от своего прогона."""
        runner = getattr(self.app, "active_runner", None)
        target = getattr(runner, "_last_target", None)
        return not target or target == (
            self._active_task_id,
            self.app._system,
        )

    def _active_run_error(self) -> str | None:
        """Ошибка наблюдаемого прогона открытого треда; чужая и ошибка
        прошлого прогона того же раннера не рисуются."""
        if not self._runner_is_for_active_thread():
            return None
        runner = self._watched_runner or getattr(
            self.app, "active_runner", None
        )
        return self._run_error_of(runner)

    def _run_error_of(self, runner) -> str | None:
        """Ошибка прогона раннера: пара «раннер и `run_seq`» прогона —
        застанного тиком, иначе текущего — совпадает с парой, в которой
        ошибка случилась. Ошибка прошлого прогона не наследуется."""
        err = getattr(self.app, "last_runner_error", None)
        if err is None:
            return None
        seq = self._watched_run_seq if runner is self._watched_runner else None
        if seq is None:
            seq = getattr(runner, "run_seq", None)
        owner = getattr(self.app, "last_runner_error_run", None)
        return err if owner == (runner, seq) else None

    def _run_task_id(self, runner) -> str | None:
        """Тред прогона раннера: его цель, иначе активный."""
        return self.app.run_thread_id(runner) or self._active_task_id

    def _adopt_thread_breaker(self, task_meta: dict) -> None:
        """Предохранитель моделей — владение треда (спека §8): restore
        ЦЕЛИКОМ заменяет breaker окна под открытый тред. Пока идёт ход,
        предохранитель принадлежит его треду — им пользуется исполнитель;
        открытый получит свой в конце хода (`_on_run_finished`)."""
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and runner.is_running():
            return
        self.app.load_circuit_breaker(read_breaker_from_meta(task_meta))

    def _on_run_finished(self, runner, kind: str) -> None:
        """Спадающий фронт прогона: исход — в мету треда прогона.

        Тред прогона считается один раз: к тику окончания человек мог уйти
        на другую сессию. Тогда экран (индикатор, сводка, баннер плана,
        подсказка поля) не трогается — он принадлежит открытому треду."""
        run_tid = self._run_task_id(runner)
        on_screen = run_tid == self._active_task_id
        edits = self._close_turn_edits(kind, run_tid, on_screen=on_screen)
        if self._store is None or run_tid is None or kind == "compact":
            return
        # Запись итога этого хода уже в файле (ядро пишет её до выхода
        # прогона): дочитать хвост до решения.
        outcome = None
        if on_screen:
            self._poll_trace_tail()
            outcome = self._latest_turn_outcome()
        if outcome is not None:
            word, plan_proposed = kind_of(outcome)
        else:
            word, plan_proposed = self._runner_outcome(runner)
        _log.debug(
            "run finished: task=%s on_screen=%s cursor=%s record=%s word=%s",
            run_tid,
            on_screen,
            self._cursor,
            outcome,
            word,
        )
        self._stamp_run_outcome(run_tid, word, edits)
        if on_screen and self._outcome_line_due(runner, word):
            self._show_turn_outcome(word, plan_proposed, outcome, edits=edits)
        self.app.last_stop_requested = False
        self._update_terminal_title()
        if not on_screen:
            # Ход чужого треда кончился: предохранитель — открытому.
            open_tid = self._active_task_id
            self._adopt_thread_breaker(
                self._store.get_task_meta(open_tid) if open_tid else {}
            )
            return
        # Ход закончился предложенным планом — баннер одобрения.
        if plan_proposed:
            # После дорисовки хвоста ленты в этом же тике: иначе рамка
            # плана была бы поставлена раньше исходной и задвоилась.
            self.call_after_refresh(self._present_session_plan)
        else:
            # статус плана мог смениться за ход (доработка, утверждение)
            self._sync_plan_panel_with_session()
            self._sync_input_placeholder()

    def _outcome_line_due(self, runner, word: str) -> bool:
        """Строка итога — когда поток хода вышел. Остановленный дорабатывает
        (мигающий «■»), новый прогон уже идёт — строку ведёт тик. Строка
        ошибки — всегда, даже пока поток раннера жив (S10 §3.7).

        Ход уложился между тиками — индикатора нет, ставим. Тик, заставший
        прогон, индикатор создал (или строка уже стоит); если нет ни того,
        ни другого, их убрали намеренно (отмена-«undo») — не воскрешаем."""
        if word == "failed":
            return True
        if runner is not None and runner.is_running():
            return False
        return (
            self._thinking is not None
            or self._fresh_turn_line() is not None
            or not self._was_running
        )

    def _runner_outcome(self, runner) -> tuple[str, bool]:
        """Исход прогона без записи итога: по ошибке раннера, стопу и
        причине ядра; True — ход кончился предложенным планом."""
        reason = getattr(self.app, "last_terminal_outcome", None)
        word = outcome_word(
            failed=self._run_error_of(runner) is not None,
            stopped=bool(getattr(self.app, "last_stop_requested", False)),
            reason=reason,
        )
        # Прямой stop в обход _cmd_stop (active_runner не обнулён) —
        # не «completed».
        if word == "completed" and getattr(runner, "_stop_requested", False):
            word = "stopped"
        return word, word == "completed" and reason == "plan_proposed"

    def _stamp_run_outcome(self, run_tid: str, word: str, edits) -> None:
        """Записать исход прогона в meta.tui треда `run_tid` (TUI-owned):
        статус — кэш для списка сессий и заголовка; сводка правок хода —
        данные TUI. Исход, время и причина хода — в записи итога ядра."""
        self._write_tui_meta(
            run_tid,
            {
                "status": meta_status_of(word),
                "last_indicator": {"edits": list(edits) if edits else None},
            },
        )

    def _poll_tick(self) -> None:
        # Потолок #trace = экран минус хром. Зависит только от стабильных высот  # noqa: ERA001  # описание формата, не выключенный код
        # (header/бар-константа/input/footer) → пересчёт синхронный и
        # без гонки (на гарде ставит max-height лишь при ресайзе / росте ввода).
        self._sync_trace_max_height()
        # Freeze UI на время app-select (§3.2 F2–F5). Пока Screen._selecting —
        # точечно глушим UI-мутации трейса (stick/scroll/stream paint/wipe),
        # но НЕ весь poll: footer/OSC/title/runner-логика/orphan/tail-события
        # без мутаций экрана продолжаются (F6 — runner жив, UI не тупеет целиком).
        _frozen = self._select_watchdog()
        # Липкость к низу: детект ручного скролла + повторное прижатие.
        # F2: при freeze — не обновляем stick и не прижимаем (текст стоит на месте).
        if not _frozen:
            self._update_stick_state()
            if self._stick_bottom:
                self._scroll_trace_end()
            # Сетка накладок сдвигается и без прокрутки (хром, смена треда).
            self._refresh_trace_overlays()
        # Отложенное удаление осиротевших реплик (cancel-with-undo), когда idle.
        self._run_orphan_cleanups()
        # Индикатор загрузки MCP — на старте (до появления активной задачи) тоже.
        self._update_mcp_indicator()
        self._pump_reasoning_stream_source()
        err = self._active_run_error()
        if err and not self._error_shown:
            # Причина — строкой `└` под «✕ Ошибка» свежей строки итога в
            # ленте. Без треда строки итога нет — тогда строкой без
            # владельца.
            if self._active_task_id is None:
                self._mount_standalone(f"runtime: {err}", "error")
            self._error_shown = True
            # Сначала дочитать хвост: реплика упавшего хода могла лечь в файл
            # до ошибки и сверится со своей отправкой; иначе пометка «не
            # отправлено» сняла бы её с ожидания, и запись смонтировалась
            # бы второй репликой.
            if not _frozen:
                self._poll_trace_tail()
            self._mark_pending_failed()
        # Инвариант: running tool-строки живут, только пока задача жива (live
        # runner / ask_human). Любой НЕ-live статус — стоп, обрыв runner'а, resume
        # брошенного треда или неизвестный исход (бейдж None) → осиротевшие вызовы
        # финализируются «прервано». Критерий «жива» = бейдж среди live-состояний.
        # Остановленный поток ещё дорабатывает — ждём его выхода: перевод
        # членов и строка «■ Отменено» встают одним тиком выхода (§2.2).
        if (
            self._active_calls
            and not self._stopping_alive()
            and self._task_status_badge() not in ("[running]", "[asking]")
        ):
            view = self._top_blocks()
            for row in self._active_calls.values():
                view.interrupted(row, started=not row.waiting)
            self._interrupt_nested_rows()
            self._active_calls.clear()
            self._nested_tool_rows.clear()
            self._subagent_progress.reset()
        # ThinkingIndicator — пока runner работает, обычно виден внизу trace.
        runner = getattr(self.app, "active_runner", None)
        is_running = runner is not None and runner.is_running()
        _kind = (
            getattr(runner, "_thread_kind", "run")
            if runner is not None
            else "run"
        )
        # Спадающий фронт — первым: новый ход того же раннера не прячет
        # конец прошлого.
        self._settle_ended_run()
        # Восходящий фронт: прогон стартовал → running (страховочная сетка к
        # _prepare_for_new_run; ловит любой путь, где prepare не вызвали).
        # Тред — прогона: к первому тику человек мог уйти в другую сессию.
        # Зафиксированный прогон (ошибка при живом потоке) — уже не работа:
        # ни восходящего фронта, ни слежения (S10 §3.7).
        live = is_running and not self._run_settled(runner)
        if (not self._was_running) and live and _kind != "compact":
            self._stamp_tui_running(self._run_task_id(runner))
        if live:
            self._watched_runner = runner
            self._watched_run_seq = getattr(runner, "run_seq", None)
            self._follow_turn_clock()
        self._was_running = live
        if not _frozen:
            # Бар — по прогону открытого треда; догон после выделения (F2).
            self._sync_indicator_bar()
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
        self._apply_human_wait(self._confirm_active or ask_human_active)
        # Nested progress субагента: drain runtime-очереди → строки под parent.
        self._drain_subagent_progress()
        summarizing = False
        finalizing = False
        finish_gate_checking = False
        _tmeta: dict = {}
        if self._store is not None and self._active_task_id is not None:
            try:
                _tmeta = self._tick_meta()
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
        if (
            self._was_summarizing
            and not summarizing
            and self._store is not None
            and self._active_task_id is not None
        ):
            live_count = (_tmeta.get("runtime") or {}).get("live_msg_count")
            if live_count is not None:
                try:
                    self._write_tui_meta(
                        self._active_task_id,
                        {"last_compact_msg_count": int(live_count)},
                    )
                except (TypeError, ValueError):
                    pass
        self._was_summarizing = summarizing

        # Инвариант: нет активной задачи (пустое состояние) → индикатора нет.
        # Без открытого треда нет и бара; итог встанет строкой по записи,
        # когда тред откроют (S10 §4.4).
        if self._active_task_id is None:
            self._destroy_thinking_indicator()
            if not _frozen:
                self._clear_stream_block()
            self._refresh_footer()
            self._update_terminal_title()
            return

        self._session_lock_heartbeat()

        _indicator_mode = select_indicator_mode(
            err=err,
            is_running=is_running,
            ask_human=ask_human_active,
            confirm=self._confirm_active,
        )
        if _indicator_mode == "failed":
            # Краш раннера: строку «✕ Ошибка» уже поставил спадающий фронт
            # этого тика (S10 §3.7), в баре ничего не рисуется.
            if not _frozen:
                self._clear_stream_block()
        elif _indicator_mode == "active":
            # summarizing → «сжимаю контекст…»; finish_gate_checking (судья
            # ответа реально сейчас вызывается) → «сверяюсь…» — ВЫШЕ защёлки
            # _finalized, иначе судья работает невидимо под «завершаю…»;
            # finalizing (принят финальный ответ: submit_solution ИЛИ
            # финальная проза — идёт запись/саммари/flush/cleanup)
            # → «✓ завершаю…»; иначе «думаю…». Sentinel «_finalized» и флаг
            # tui_finalizing оба дают validating (см. select_active_substate).
            substate = select_active_substate(
                summarizing=summarizing,
                finalizing=finalizing,
                finish_gate_checking=finish_gate_checking,
                last_event_kind=self._last_event_kind,
            )
            self._show_thinking_indicator(state=substate)
            # Эфемерный StreamBlock — live-вывод текущего шага. Источник —
            # app._stream_sink (кормится из worker-потока через on_stream_delta);
            # монтаж/обновление строго здесь, на UI-потоке (set_interval-колбэк).
            # StreamBlock — только в активной фазе «thinking». В validating
            # (финальный ответ виден, идёт finish-gate) и compressing reasoning
            # не показываем — иначе строка-мысль висит после финального ответа (баг B).
            _thinking_phase = (
                (not summarizing)
                and (not finalizing)
                and (self._last_event_kind != "_finalized")
            )
            sink = getattr(self.app, "_stream_sink", None)
            # F3: при freeze не монтируем/не красим стрим-виджеты (mount/switch/
            # set_content дрожали бы экран под выделением). Контент догонит после MouseUp.
            if (
                _thinking_phase
                and sink is not None
                and sink.has_content()
                and not _frozen
            ):
                from gc_tui.services.markdown_render import render_prose
                from gc_tui.services.stream_sink import (
                    format_reasoning_line,
                    format_stream_block,
                    select_stream_render,
                )
                from gc_tui.widgets.stream_block import StreamBlock

                think, answer = sink.snapshot()
                mode = self.app.base_settings().settings.tui.stream_display
                clean = format_stream_block(answer=answer)
                reasoning = format_reasoning_line(think=think)
                kind, thinking = select_stream_render(mode, clean, reasoning)
                from gc_tui.widgets.trace_row import TraceRow

                if kind == "answer":
                    if (
                        self._stream_block is not None
                    ):  # switch reasoning→answer
                        self._stream_block.remove()
                        self._stream_block = None
                    if self._stream_answer_row is None:
                        self._stream_answer_row = TraceRow(
                            prefix="● ",
                            content=render_prose(clean),
                            kind_class="line-agent",
                            focusable=False,
                        )
                        self._trace_mount(self._stream_answer_row, scroll=True)
                    self._refresh_stream_content()
                elif kind == "reasoning":
                    if (
                        self._stream_answer_row is not None
                    ):  # switch answer→reasoning
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
            # анимация стоит и вернётся, когда агент продолжит.
            self._hide_thinking_indicator()
        else:  # terminal
            # Idle. Исход прогона — строка итога в ленте (S10 §3.3), бар и
            # индикатор уходят; мигающий «■» стопа — пока поток жив.
            self._show_idle_indicator(runner, frozen=_frozen)
            if not _frozen:
                self._clear_stream_block()
        # Footer-left зависит от is_running — обновляем каждый тик, чтобы
        # /stop появлялся/исчезал без отдельных вызовов из start/stop путей.
        self._refresh_footer()
        self._update_terminal_title()  # статус живой в табе: [running]→[completed] и т.д.
        # F3: при freeze хвост не читаем и трейс НЕ перерисовываем
        # (wipe/clear_trace/render_records дрожали бы экраном под выделением):
        # курсор стоит, первый тик после MouseUp дочитает накопленный хвост,
        # сброс или шаг сжатия. Пометки ленты — после записей тика: строка
        # вызова-владельца могла прийти в этом же хвосте.
        if not _frozen:
            self._poll_trace_tail()
            self._drain_trace_notes()
            # Ход кончился, хвост дочитан: впущенное уже сверено. Остальное
            # ядро может держать до следующего прогона (сообщение во время
            # `/compact`) — в поле только снятое у него. После стопа
            # раннера нет: очередь разобрал стоп.
            if (
                runner is not None
                and not is_running
                and self._runner_is_for_active_thread()
            ):
                self._return_queue_to_input(self._withdraw_queue(runner))

    def _show_idle_indicator(self, runner, *, frozen: bool) -> None:
        """Idle: бар уходит, исход — строкой итога в ленте.

        Остановленный ход дорабатывает — мигающий «■». Запись итога
        последнего хода — строка из неё (стоп: время — момент стопа).
        Исход без записи уже показан спадающим фронтом; иначе — по статусу
        сессии; неизвестный исход строки не даёт, индикатор уходит."""
        self._sync_indicator_bar()
        ind = self._thinking
        if ind is None:
            return
        if self._stopping_alive():
            ind.show("stopping")
            return
        if ind.state == "stopping" and not frozen:
            # Поток стопа вышел: его запись уже в файле — до решения.
            self._poll_trace_tail()
        outcome = self._latest_turn_outcome()
        if outcome is not None:
            self._show_turn_outcome(*kind_of(outcome), outcome)
            return
        word = self._idle_status_word(runner)
        if word is None:
            self._destroy_thinking_indicator()
        else:
            self._show_turn_outcome(word, False, None)

    def _idle_status_word(self, runner) -> str | None:
        """Слово исхода без записи по статусу сессии (`tui.status`), с
        guard прямого stop в обход `_cmd_stop`; лимитного исхода статус не
        различает."""
        status = None
        if self._store is not None and self._active_task_id is not None:
            status = (self._tick_meta().get("tui") or {}).get("status")
        word = word_of_status(status)
        if word == "completed" and getattr(runner, "_stop_requested", False):
            word = "cancelled"
        return word

    def _follow_turn_clock(self) -> None:
        """Живой счёт индикатора хода — от часов хода ядра: ожидание
        человека в него не входит, ответ на вопрос агента его не обнуляет.
        Пока часов нет (сборка исполнителя, хвост из очереди до нового
        хода) — счёта нет. Не ход (`/compact`) или раннер без часов — счёт
        от отправки, как был."""
        runner = getattr(self.app, "active_runner", None)
        probe = getattr(runner, "turn_effective_start", None)
        if (
            self._thinking is None
            or probe is None
            or not runner.is_running()
            or getattr(runner, "_thread_kind", "run") != "run"
        ):
            return
        self._thinking.set_started_at(probe())

    def _poll_trace_tail(self) -> None:
        """Дочитать и отрисовать хвост записей активного треда."""
        if (
            self._store is None
            or self._active_task_id is None
            or self._cursor is None
        ):
            if self._active_task_id is not None:
                _log.debug(
                    "trace tail skipped: task=%s store=%s cursor=%s",
                    self._active_task_id,
                    self._store is not None,
                    self._cursor,
                )
            return
        tail = self._store.tail_events(self._active_task_id, self._cursor)
        if (
            tail.new_records
            or tail.stream_reset
            or tail.compaction is not None
        ):
            # Наблюдение смоука (живой экран не показал ход после ошибки):
            # что прочитано и откуда — только debug, тик идёт 4 раза в с.
            _log.debug(
                "trace tail: task=%s cursor=%s next=%s records=%d "
                "reset=%s compaction=%s",
                self._active_task_id,
                self._cursor,
                tail.next_cursor,
                len(tail.new_records),
                tail.stream_reset,
                tail.compaction is not None,
            )
        if tail.compaction is not None:
            self._apply_compaction(tail.compaction, tail.next_cursor)
            return
        if tail.stream_reset:
            self._rebuild_from_store()
            return
        self._cursor = tail.next_cursor
        self._render_records(tail.new_records, live=True)
        self._refresh_header()

    def _rebuild_from_store(self) -> None:
        """Полная перерисовка трейса из хранилища: сброс потока и откат
        неудавшегося шага сжатия."""
        tid = self._active_task_id
        self._clear_trace()
        self._reset_render_state()
        records, cur = self._store.read_all_events(tid)
        self._cursor = cur
        if records and self._store.is_compaction_summary(records[0]):
            self._render_segments(self._store.read_segments(tid))
        else:
            self._segment_starts = [0]
            self._render_records(records)
        self._merge_sidecar_nested(tid)
        # После компакции (stream_reset) баннер мог пере-монтироваться для уже
        # отвеченного вопроса — снять, иначе ввод уйдёт в stdin «вникуда».
        self._reconcile_ask_human_after_render(allow_remnant=False)
        # I1/A: GC pending симметрично attach — delivered drop, orphan → failed.
        self._mount_failed_from_meta(tid)
        # Лента перерисована посреди хода: рамки плана новые, их состояние
        # берётся из сессии заново.
        self._sync_plan_panel_with_session()
        self._refresh_header()
        self._refresh_footer()

    def _apply_compaction(
        self, step: CompactionStep, next_cursor: TaskCursor
    ) -> None:
        """Шаг сжатия — обычная порция записей плюс маркер (спека 3.2).

        Строки до сжатия, карты экрана, индикатор, баннер подтверждения и
        прокрутка остаются. Сбой — откат полной перерисовкой."""
        try:
            self._append_compaction_step(step)
        except Exception:
            _log.exception("шаг сжатия не применён, трейс перерисован")
            self._rebuild_from_store()
            return
        self._cursor = next_cursor
        self._refresh_header()

    def _append_compaction_step(self, step: CompactionStep) -> None:
        """Хвост архива, маркер, новый живой файл — живым путём, одним
        контекстом рендера (вызов в хвосте сводится с выводом в живом)."""
        start = len(self._task_events)
        context = _records_context(
            [*step.archive_tail, *step.live_records], self._current_cwd()
        )
        self._render_records(step.archive_tail, live=True, context=context)
        self._mount_compaction_marker(step)
        self._segment_starts.append(len(self._task_events))
        self._render_records(step.live_records, live=True, context=context)
        self._lighten_segment_left_window(step)
        self._drop_answered_ask_human_banner(start)

    def _lighten_segment_left_window(self, step: CompactionStep) -> None:
        """Retention: сегмент, выпавший из окна, облегчаем как полная
        перерисовка. Вложенные строки субагентов не трогаем (спека 3.4)."""
        for ev in events_leaving_window(
            self._task_events, self._segment_starts, step.window_ids
        ):
            apply_payload_retention(ev, seg_in_window=False)

    def _mount_compaction_marker(self, step: CompactionStep) -> None:
        no = self._next_no
        self._append_and_render(
            build_compaction_marker_event(
                no, step.segment_index, step.compacted_by
            )
        )
        marker = self._summary_marker_widgets.get(no)
        runner = getattr(self.app, "active_runner", None)
        if (
            marker is not None
            and getattr(runner, "_thread_kind", "run") == "compact"
            and self._bar_wanted()
        ):
            # Маркер `/compact` встаёт вместе с уходом бара «сжимаю
            # контекст…»; сжатие посреди хода бар не снимает (S10 §3.4).
            marker.add_class(TURN_LINE_HELD)
            self._held_reveals.append(
                partial(marker.remove_class, TURN_LINE_HELD)
            )

    def _drop_answered_ask_human_banner(self, start: int) -> None:
        """Снять баннер ask_human, если среди событий шага (с индекса start)
        после вопроса диалог продолжился: ответ ушёл в сводку и отдельной
        записью не придёт. Маркер сжатия — не продолжение: агент, который
        реально ждёт ответа, баннер сохраняет."""
        if self._ask_human_banner is None:
            return
        added = self._task_events[start:]
        ah_idx = self._last_ask_human_index(added)
        after = 0 if ah_idx is None else ah_idx + 1
        if any(ev.kind in _DIALOG_CONTINUATION_KINDS for ev in added[after:]):
            self._unmount_ask_human_banner()

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
        trace = self.query_one(_SEL_TRACE, VerticalScroll)
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
        tagline = Static(
            branding.tagline_under_logo(),
            classes="brand-tagline",
            markup=False,
        )
        # Версия — своей строкой под тэглайном, с воздухом над ней (CSS): карточка это
        # визитка продукта, и номер читается там же, где имя. Рядом же появляется
        # «↑ доступна новая версия» — текущая версия и предложение обновиться
        # складываются в одну мысль.
        version = Static(
            branding.version_under_logo(),
            classes="brand-version",
            markup=False,
        )
        left = Vertical(logo, tagline, version, id="brand-left")
        inner = Horizontal(left, self._mcp_status_row, id="brand-row")
        self._brand_row = inner  # раскладку ставит _apply_brand_layout ниже
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
        from gc_tui import branding

        lines = self._compose_card_lines(
            self._last_mcp_card_line
        ).plain.splitlines()
        need = max((len(line) for line in lines), default=0)
        logo_w = max(
            (len(line) for line in branding.LOGO.splitlines()), default=0
        )
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
            toggle = (
                self._mcp_status_row.remove_class
                if wide
                else self._mcp_status_row.add_class
            )
            toggle("brand-mcp-stacked")
        self._brand_wide = wide  # только после успешного применения

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
        return bool(self.app.base_settings().settings.mcp.servers)

    def _compose_card_lines(self, mcp_line: Optional[str]) -> Text:
        """Итог-содержимое правой колонки карточки: до четырёх строк через пустую строку.

            ⚠ MCP: 3/5 подключены · /mcp             (только если заданы MCP-серверы)

            ⚠2 ℹ1 · F4/notices                  (только если есть уведомления)

            ↑ доступна новая версия · /settings → Обновление  (teal, если авто-чек нашёл)

            ✓ готов к работе                         (всегда)

        Строка уведомлений — голый бейдж `format_notices_badge`, без слова-префикса:
        тот же вид, что в футере и на /sessions (единый источник — notices_ui).
        Детали уведомлений — на экране /notices (F4); трейс держим чистым."""
        notices = self._collect_notices()
        lines = []  # (text, style|None)
        if mcp_line:
            lines.append((mcp_line, None))
        badge = format_notices_badge(notices)
        if badge:
            lines.append(
                (badge, None)
            )  # строка целиком — базовым цветом карточки
        if getattr(self.app, "_update_available", False):
            lines.append(
                ("↑ доступна новая версия · /settings → Обновление", _ACCENT)
            )
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
            from gc_core import upgrade_gate
            from gc_core.startup_notices import warning_toasts

            messages, summary, codes = warning_toasts(
                self.app._startup_notices()
            )
            last = frozenset(
                upgrade_gate.read_state().get("last_shown_warning_codes") or []
            )
            if codes == last:
                return  # набор тот же — молчим (постоянная видимость в карточке)
            upgrade_gate.write_state(
                {"last_shown_warning_codes": sorted(codes)}
            )
            for msg in messages:
                self.app.notify(msg, severity="warning", timeout=6.0)
            if summary:
                self.app.notify(summary, severity="warning", timeout=6.0)
        except Exception:
            pass  # тосты — best-effort, не роняют старт

    def _render_startup_notices(self) -> None:
        """Boot-оркестрация карточки. Без MCP-серверов данные полны сразу →
        выставляем карточку. С MCP-серверами откладываем до settle
        (см. _settle_mcp_row), где mcp_status уже осел."""
        if self._has_mcp_servers():
            return
        self._refresh_notices_card(mcp_line=None)
        self._maybe_toast_warnings()

    def _refresh_notices_card(self, *, mcp_line: Optional[str] = None) -> None:
        """Обновить правую колонку карточки: MCP + счётчик уведомлений + готовность."""
        if self._mcp_status_row is None:
            return
        if mcp_line is not None:
            self._last_mcp_card_line = mcp_line
        self._mcp_status_row.update(
            self._compose_card_lines(self._last_mcp_card_line)
        )
        # Строки статусов только что могли стать длиннее (осели MCP, пришли
        # уведомления) — раскладку пересчитываем по НИМ, иначе колонка осталась бы
        # сбоку и рвала бы свежий текст по буквам.
        try:
            self._apply_brand_layout(
                self._brand_fits_beside(self.app.size.width)
            )
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
        trace = self.query_one(_SEL_TRACE, VerticalScroll)
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
        return any(
            w is not None
            for w in (
                self._palette,
                self._skill_picker,
                self._arg_picker,
                self._file_picker,
            )
        )

    # R1: алиасы Ctrl+R для ручного переименования активной сессии.
    # Локальный маппинг (образец — diff.py _KEY_ALIASES); diff-модуль не тащим.
    # ctrl+к / ctrl+К — русская раскладка (клавиша «R» на той же физ. кнопке).
    _RENAME_KEYS = frozenset({"ctrl+r", "ctrl+R", "ctrl+к", "ctrl+К"})

    def on_key(self, event: events.Key) -> None:
        # R1: Ctrl+R (и алиасы раскладки) — переименование активной сессии.
        # Гейт _active_task_id + _store; до confirm-banner/pickers (как в спеке).
        if (
            event.key in self._RENAME_KEYS
            and self._active_task_id is not None
            and self._store is not None
        ):
            self._rename_active_session()
            event.stop()
            event.prevent_default()
            return

        # Баннер одобрения плана: тот же приоритет и та же механика, что у
        # confirm-баннера. Esc — только клавиша: снимает баннер без действий.
        if self._plan_banner is not None:
            key = event.key
            banner = self._plan_banner
            if key == "up":
                banner.move_selection(-1)
            elif key == "down":
                banner.move_selection(1)
            elif key == "enter":
                self._apply_plan_choice(banner.selected_choice())
            elif key == "escape":
                self._hide_plan_banner()
            elif not banner.scroll_body(key):
                direct = banner.choice_for_key(key)
                if direct is not None:
                    self._apply_plan_choice(direct)
            # иначе — проглотить без действия (нет случайного ввода)
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
                self._answer_confirm(
                    banner.selected_choice()
                    if banner is not None
                    else "deny_once"
                )
            elif banner is not None and banner.scroll_body(key):
                pass  # прокрутка тела — не ответ на вопрос
            else:
                # Same physical-key sets as CLI / confirm_port (QWERTY + ЙЦУКЕН).
                from gc_core.security.confirm_port import (
                    CONFIRM_DENY_ONCE,
                    CONFIRM_DENY_REMEMBER,
                    CONFIRM_KEY_N,
                    CONFIRM_KEY_O,
                    CONFIRM_KEY_T,
                    CONFIRM_KEY_Y,
                    normalize_confirm_key,
                )

                # Esc — отмена, то есть разовый запрет: запрет на сессию
                # сильнее режима, его выбирают явно (клавишей N).
                if key == "escape":
                    self._answer_confirm(CONFIRM_DENY_ONCE)
                elif key in CONFIRM_KEY_N:
                    self._answer_confirm(CONFIRM_DENY_REMEMBER)
                elif (
                    key in CONFIRM_KEY_Y
                    or key in CONFIRM_KEY_O
                    or key in CONFIRM_KEY_T
                ):
                    self._answer_confirm(normalize_confirm_key(key))
            # иначе — проглотить без действия (нет случайного ввода)
            event.stop()
            event.prevent_default()
            return

        # Любая не-Esc клавиша сбрасывает pending hint (без прерывания обработки) —
        # и esc-confirm, и содержательный отказ: раз человек продолжил работать,
        # подсказка прочитана, футеру пора показывать актуальное.
        if event.key != "escape" and (
            self._esc_confirm_kind is not None or self._footer_refusal_active
        ):
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
        # ↑ из очереди — раньше истории (спека P3 §2.4): в пустом поле.
        if event.key == "up" and self._withdraw_last_queued():
            event.stop()
            event.prevent_default()
            return

        if event.key == "up" and not self._any_picker_open:
            try:
                input_field = self.query_one(_SEL_CMDINPUT, CommandTextArea)
                if (
                    input_field.has_focus
                    and input_field.cursor_on_first_visual_line()
                ):
                    self._history_prev()
                    event.stop()
                    event.prevent_default()
                    return
            except Exception:
                pass

        if event.key == "down" and not self._any_picker_open:
            try:
                input_field = self.query_one(_SEL_CMDINPUT, CommandTextArea)
                if (
                    input_field.has_focus
                    and input_field.cursor_on_last_visual_line()
                ):
                    self._history_next()
                    event.stop()
                    event.prevent_default()
                    return
            except Exception:
                pass

        # Esc — единая лестница приоритетов: оверлей (Esc-блоки пикеров ниже) →
        # bang-shell cancel → 2-й-Esc-confirm → идёт вставка изображения
        # (двухшаговая отмена) → текст в инпуте (двухшаговая очистка) → ask_human →
        # running (undo-cancel / stop-hint) → выход.
        # Esc всегда разбирает самый локальный контекст; безусловный стоп —
        # /stop или Ctrl+C.
        if (
            event.key == "escape"
            and len(self.app.screen_stack) == 1
            and not self._any_picker_open
        ):
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

            # Поиск файлов в полёте, пикер ещё не показан: Esc отменяет сеанс — иначе
            # результат открыл бы пикер уже после «отмены». Стоит до очистки текста:
            # отмена поиска локальнее, чем «стереть набранное».
            if (
                self._file_query is not None
                and self._file_shown_query != self._file_query
            ):
                self._close_file_picker()
                event.stop()
                event.prevent_default()
                return

            # 2-й Esc в окне 2s — выполнить отложенный action.
            if self._esc_confirm_kind is not None:
                kind = self._esc_confirm_kind
                self._clear_footer_hint()
                if kind == "clear":
                    try:
                        inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
                        inp.text = ""
                        inp._clear_chip_state()
                    except Exception:
                        pass
                elif kind == "stop":
                    self._cmd_stop()
                elif kind == "paste":
                    self._cancel_image_paste()
                event.stop()
                event.prevent_default()
                return

            # Идёт вставка изображения — самый локальный контекст: двойной Esc отменяет
            # её (чтение буфера или сетевого диска могло повиснуть), текст поля не трогает.
            if self._image_paste_busy:
                self._show_esc_hint("paste")
                event.stop()
                event.prevent_default()
                return

            input_text = ""
            try:
                input_text = self.query_one(_SEL_CMDINPUT).text
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
                # Откат отправки: агент ещё ничего не выдал → сразу, без hint.
                if self._can_cancel_with_undo():
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
                self._skill_picker.move_selection(
                    -1 if event.key == "up" else 1
                )
                event.stop()
                event.prevent_default()
                return
            if event.key == "tab":
                skill = self._skill_picker.selected_skill()
                if skill is not None:
                    inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
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
                    inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
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
                self._file_picker.move_selection(
                    -1 if event.key == "up" else 1
                )
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
            inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
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
                    self._show_footer_refusal(f"✗ не сохранено: {exc}")
                else:
                    self._palette.refresh_toggle_state()
                event.stop()
                event.prevent_default()
            # иначе — обычная строка: не перехватываем, ←/→ уходит в TextArea (каретка)
        elif event.key == "tab":
            cmd = self._palette.selected_command()
            inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
            if cmd is not None:
                inp.text = f"/{cmd.name} "
                inp.move_cursor(inp.document.end)
                self._close_palette()
            else:
                self._insert_palette_skill(inp, inp.text)
            event.stop()
            event.prevent_default()

    async def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "cmdinput":
            return
        # Подпись плашки «в конец» и её место (поле могло вырасти).
        self.call_after_refresh(self._refresh_trace_overlays)
        # Печать в input после 1-го Esc сбрасывает pending hint — клавиша уже
        # «не Esc», подтверждать нечего. Screen.on_key для печатных не вызывается
        # (TextArea их потребляет), поэтому ловим здесь. Содержательный отказ —
        # то же правило (человек продолжил печатать), НО только на непустом
        # тексте: `inp.text = ""` в командах стоит ДО исполнения, а Changed от
        # него доставляется ПОСЛЕ — когда отказ уже показан, и пустое изменение
        # гасило его в момент появления (живой смок владельца 2026-09-04).
        if self._esc_confirm_kind is not None or (
            self._footer_refusal_active
            and event.text_area.text
            and event.text_area.text != self._refusal_input_text
        ):
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
                self._close_file_picker()  # ушли из файлового ввода: поиск в полёте больше не нужен
                # /skill <валидный_id> <текст> → скилл зафиксирован, дальше промпт:
                # закрываем picker (его фильтр по 'id текст' бессмыслен).
                if self._skills_cache is None:
                    self._skills_cache = await self._load_skills_safe()
                skill_first, _, skill_rest = rest.partition(" ")
                if skill_rest.strip() and isinstance(
                    self._resolve_cached_skill(skill_first), Resolved
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
                    await self._open_skill_picker(prefix=rest)
                else:
                    self._skill_picker.set_filter(rest)
                return

            # Состояние 3: /<cmd> <фильтр>, где у команды есть arg_options
            # (статические или динамические через arg_options_provider) → arg-picker.
            cmd_obj = self._registry.lookup(cmd)
            if cmd_obj is not None and (
                cmd_obj.arg_options or cmd_obj.arg_options_provider is not None
            ):
                self._close_file_picker()  # ушли из файлового ввода: поиск в полёте больше не нужен
                if self._suppress_picker_reopen:
                    self._suppress_picker_reopen = False
                    return
                if self._palette is not None:
                    self._close_palette()
                if self._skill_picker is not None:
                    self._close_skill_picker()
                if self._arg_picker is None:
                    options = (
                        list(cmd_obj.arg_options)
                        if cmd_obj.arg_options
                        else list(cmd_obj.arg_options_provider(self.app))
                    )
                    hint_provider = None
                    if cmd_obj.arg_hint_provider is not None:
                        provider = cmd_obj.arg_hint_provider
                        hint_provider = lambda value: provider(self.app, value)
                    self._open_arg_picker(
                        options=options,
                        prefix=rest,
                        hint_provider=hint_provider,
                    )
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
        from gc_tui.services.file_index import parse_at_segment

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
        self._close_file_picker()

        # Иначе: все закрыты.
        if self._palette is not None:
            self._close_palette()
        if self._skill_picker is not None:
            self._close_skill_picker()
        if self._arg_picker is not None:
            self._close_arg_picker()

    _MIN_TRACE_WITH_OVERLAY = (
        3  # минимум строк трейса видно при открытом оверлее
    )
    _FILE_SEARCH_NOTICE_DELAY = (
        0.3  # сек: после них пикер показывает «ищу по проекту…»
    )
    _FILE_LISTING_LIMIT = (
        100  # строк в листинге папки; остальное — «допишите часть имени»
    )

    def _open_overlay_widget(self):
        """Открытый input-оверлей (палитра/skill/arg/file-picker) или None."""
        return (
            self._palette
            or self._skill_picker
            or self._arg_picker
            or self._file_picker
        )

    def _mount_input_overlay(self, widget) -> None:
        """Смонтировать input-оверлей (палитра / skill/arg/file-picker) СРАЗУ ПОД
        полем ввода — список раскрывается ВНИЗ (новая раскладка: ввод следует за
        контентом, под ним есть место). Потолок оверлея и ужатие #trace под его
        желаемую высоту считает _sync_trace_max_height (по virtual_size оверлея);
        отложенный вызов даёт это сразу, не дожидаясь poll-тика."""
        self.query_one(_SEL_ROOT, Vertical).mount(
            widget, after=self.query_one(_SEL_INPUT_ROW, Horizontal)
        )
        self.call_after_refresh(self._sync_trace_max_height)

    def _open_palette(self, *, prefix: str) -> None:
        if self._palette is not None:
            return
        self.spend_help_hint()
        palette = CommandPalette(
            self._registry,
            initial_filter=prefix,
            toggle_state=lambda name: toggle_state(self.app, name),
            toggle_extra=lambda name: preset_track_extra(self.app, name),
            toggle_custom_hint=lambda name: preset_custom_hint(self.app, name),
            toggle_dynamic_hint=lambda name, value: dynamic_toggle_hint(
                self.app, name, value
            ),
            toggle_values=lambda name: effective_toggle_values(self.app, name),
            skills=self._palette_skills,
        )
        self._palette = palette
        self._mount_input_overlay(palette)

    def _close_palette(self) -> None:
        if self._palette is None:
            return
        self._palette.remove()
        self._palette = None

    async def _open_skill_picker(self, *, prefix: str) -> None:
        if self._skill_picker is not None:
            return
        if self._skills_cache is None:
            self._skills_cache = await self._load_skills_safe()
        from gc_tui.widgets.skill_picker import SkillPicker

        picker = SkillPicker(self._skills_cache, initial_filter=prefix)
        self._skill_picker = picker
        self._mount_input_overlay(picker)

    def _close_skill_picker(self) -> None:
        if self._skill_picker is None:
            return
        self._skill_picker.remove()
        self._skill_picker = None

    def _open_arg_picker(
        self,
        *,
        options: list[tuple[str, str]],
        prefix: str,
        hint_provider=None,
    ) -> None:
        if self._arg_picker is not None:
            return
        from gc_tui.widgets.arg_picker import ArgPicker

        picker = ArgPicker(
            options, initial_filter=prefix, hint_provider=hint_provider
        )
        self._arg_picker = picker
        self._mount_input_overlay(picker)

    def _open_profiles_picker(self, name: str = "profiles") -> None:
        """Раскрыть пикер /profiles (спека §12.1, ревизия 2026-07-28): текст
        `/profiles ` в input → on_text_area_changed (состояние 3) сам откроет
        ArgPicker с динамическими опциями и хинтом состава слотов."""
        inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
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

    def _show_file_picker(self, *, query: str) -> None:
        """Открыть/обновить файловый пикер по query.

        Листинг одной папки (пустой query, хвостовой '/') — дешёвое чтение диска,
        показываем сразу. Поиск по проекту — только в фоне: сборка индекса на большом
        проекте занимает секунды, в UI-потоке это фриз всего приложения (и Ctrl-C в
        том числе). Считает его один поток сеанса (FileSearchSession): нажатие только
        отдаёт ему запрос. Результат применяет _on_file_search_done, если ввод за это
        время не ушёл дальше."""
        from gc_tui.services.file_index import PathIndex, list_dir
        from gc_tui.services.file_search import FileSearchSession

        root = self._project_root()
        if (
            self._file_query is None
            and self._path_index is not None
            and self._path_index.built
        ):
            # Новый сеанс: индекс прошлого сеанса достроился уже после его закрытия.
            self._path_index = None
        self._file_query = query
        if not query or query.endswith("/"):
            entries = list_dir(root, query[:-1])
            self._apply_file_entries(
                entries[: self._FILE_LISTING_LIMIT],
                truncated=False,
                total=len(entries),
            )
            return
        index = self._path_index
        if index is None or index.root != root:
            index = self._path_index = PathIndex(root)
        search = self._file_search
        if search is None or search.index is not index:
            if search is not None:
                search.close()  # корень сменился посреди сеанса
            search = self._file_search = FileSearchSession(
                index, partial(self._post_file_search_result, index)
            )
        search.want(query)
        self.set_timer(
            self._FILE_SEARCH_NOTICE_DELAY,
            lambda: self._show_file_search_pending(index, query),
        )

    def _post_file_search_result(
        self, index, query: str, entries: list, truncated: bool
    ) -> None:
        """Результат поиска — в UI-поток. Выполняется в потоке поиска.

        Подсказка «поиск неполный» не нужна, если запрос называет существующую папку:
        человек уже уточнил путь, а её прямых детей search дочитывает с диска целиком
        (совпадения по подстроке в других папках при этом могли в обрезанный индекс не
        попасть — осознанно: вечная подсказка на огромном проекте хуже). Проверка
        диска — здесь, в потоке поиска, не в UI."""
        from gc_tui.services.file_index import names_existing_dir

        try:
            truncated = truncated and not names_existing_dir(index.root, query)
            self.app.call_from_thread(
                self._on_file_search_done, index, query, entries, truncated
            )
        except Exception:
            # Приложение уже закрыто (показывать некуда), сбой проверки диска или сбой
            # в обработчике экрана — ни один не должен пропасть молча.
            _log.warning(
                "file search result dropped: root=%s query=%r",
                index.root,
                query,
                exc_info=True,
            )

    def _on_file_search_done(
        self, index, query: str, entries: list, truncated: bool
    ) -> None:
        if index is not self._path_index or query != self._file_query:
            return  # сеанс сменился / ввод ушёл дальше
        self._apply_file_entries(entries, truncated=truncated)

    def _show_file_search_pending(self, index, query: str) -> None:
        """Поиск идёт дольше порога — показать «ищу по проекту…» вместо списка.
        Без этого долгая первая сборка индекса выглядит как «ничего не сработало»."""
        if index is not self._path_index or query != self._file_query:
            return  # сеанс закончился или ввод ушёл дальше
        if self._file_shown_query == query:
            return  # результат уже на экране
        self._apply_file_entries([], truncated=False, searching=True)

    def _apply_file_entries(
        self,
        entries: list,
        *,
        truncated: bool,
        searching: bool = False,
        total: Optional[int] = None,
    ) -> None:
        """Совпадений нет → скрыть.

        Пустой ящик «(нет совпадений)» не показываем: выбрать из него нечего,
        а информация «не нашлось» видна по отсутствию списка. Пикер сам
        возвращается, как только поиск снова что-то находит (состояния
        пересчитываются на каждый Changed). Индекс при этом НЕ сбрасываем —
        сеанс пикера продолжается, опечатка не должна стоить пересборки.

        Исключение — обрезанный индекс (truncated): пустой ящик с подсказкой
        показываем. Файла может не быть в индексе, и молчащий пикер читался бы
        как «такого файла нет».

        Режим searching (долгий фоновый поиск ещё не вернулся): показываем
        строку «ищу по проекту…» вместо прежнего списка. Это не список для
        текущего запроса — _file_shown_query НЕ обновляем, иначе Tab/Enter
        достроили бы путь по строке-заглушке (_complete_file_picker сверяет
        _file_shown_query с _file_query)."""
        if not searching:
            # На обоих путях вызова _file_query уже равен применяемому запросу.
            self._file_shown_query = self._file_query
        if not entries and not truncated and not searching:
            if self._file_picker is not None:
                self._file_picker.remove()
                self._file_picker = None
            return
        if self._file_picker is None:
            from gc_tui.widgets.file_picker import FilePicker

            picker = FilePicker(
                entries, truncated=truncated, searching=searching, total=total
            )
            self._file_picker = picker
            self._mount_input_overlay(picker)
        else:
            self._file_picker.set_entries(
                entries, truncated=truncated, searching=searching, total=total
            )

    def _close_file_picker(self) -> None:
        # Конец сеанса: результат поиска в полёте устарел, поток сеанса больше не нужен.
        self._file_query = None
        self._file_shown_query = None
        if self._file_search is not None:
            self._file_search.close()
            self._file_search = None
        # Достроенный индекс сбрасываем: следующий «@» соберёт заново (появятся
        # файлы, созданные агентом). Ещё строящийся оставляем — свежее некуда, а
        # сброс запустил бы вторую сборку параллельно первой (её не остановить).
        if self._path_index is not None and self._path_index.built:
            self._path_index = None
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
        if self._file_shown_query != self._file_query:
            # Поиск по новому вводу ещё в полёте, на экране список для ПРЕЖНЕГО
            # запроса: достройка по нему подменила бы набранное чужим путём.
            return
        entry = self._file_picker.selected_entry()
        if entry is None:
            return
        inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
        row, col = inp.cursor_location
        lines = inp.text.split("\n")
        line = lines[row]

        first, sep, _rest = line.partition(" ")
        cmd_obj = (
            self._registry.lookup(first[1:])
            if sep and first.startswith("/")
            else None
        )
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

        from gc_tui.services.file_index import parse_at_segment

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

    def _is_command_input(self, text: str) -> bool:
        """Набранное — команда (`/…`, `!…`), а не текст агенту.

        Поле одно на ответ и команды. Пока висит вопрос агента, командой
        считается только известная `/команда` или `/имя` скилла из прогретого
        кэша (холодный кэш — ответ); прочее с «/» (путь и т.п.) и с «!» —
        ответ. Вне вопроса любое `/…` — команда (неизвестная получит
        «нет такой команды»), `!…` — shell.
        """
        asking = self.app.tui_state.ask_human_state is not None
        if text.startswith("/"):
            name = (text[1:].split(None, 1) or [""])[0]
            return not asking or self._is_known_command(name)
        return text.startswith("!") and not asking

    def _is_known_command(self, name: str) -> bool:
        """`/имя` запустит команду (или алиас) или скилл — точный разбор
        по прогретому кэшу, как при запуске; команда сильнее скилла."""
        return self._registry.lookup(name) is not None or isinstance(
            self._resolve_cached_skill(name), Resolved
        )

    async def on_command_text_area_submitted(
        self, event: CommandTextArea.Submitted
    ) -> None:
        inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
        # Запись голосом: Enter = стоп+распознать (НЕ отправка сообщения).
        if self._voice is not None and self._voice.state == "recording":
            self._voice.stop_and_transcribe()
            return

        # Идёт вставка изображения: Enter не отправляет — сообщение ушло бы без
        # картинки; текст остаётся в поле (контракт §2).
        if self._image_paste_busy:
            self._show_footer_refusal(IMAGE_PASTE_WAIT_HINT)
            return

        # Картинки с командой не отправляются (визуальный контракт картинок §2):
        # отказ раньше пикеров и палитры — они очищают ввод, а набранное с
        # плашками должно остаться в поле.
        images = event.attachments
        if images and self._is_command_input(event.value.strip()):
            self._show_footer_refusal(IMAGES_WITH_COMMAND_REFUSAL)
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
                await self._handle_command(f"{cmd_name} {skill['id']}")
            elif text.startswith("/"):
                # Пустой результат фильтра — выполнить ввод как обычную команду.
                await self._handle_command(text[1:])
            return

        # Поиск файлов в полёте: Enter игнорируется, что бы ни было на экране
        # (прежний список, «ищу по проекту…» или ещё ничего) — как и Tab. Иначе ушло
        # бы сообщение с недописанным «@…», а вложение молча потерялось. Нажатие не
        # запоминаем: достраивать путь по списку, которого человек ещё не видел,
        # хуже лишнего Enter. Выход — Esc, после неё Enter отправляет набранное как есть.
        if (
            self._file_query is not None
            and self._file_shown_query != self._file_query
            and self._arg_picker is None
            and self._palette is None
        ):
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
                await self._handle_command(f"{cmd_name} {arg}".strip())
            return

        # Если открыта палитра — Enter выполняет выбранную/набранную команду
        # (логика перенесена из on_key, т.к. TextArea перехватывает Enter).
        # Команда с обязательным аргументом или меню вариантов (`<path>`,
        # `[txt|…]`) подставляется в поле — выполнение следующим Enter;
        # необязательный аргумент без меню (`/plan`) выполняется сразу
        # (`Command.runs_on_enter`).
        if self._palette is not None:
            if self._insert_palette_skill(inp, event.value):
                return
            cmd = self._palette.selected_command()
            if cmd is not None and cmd.is_toggle:
                spec = TOGGLES.get(cmd.name)
                if spec is not None and spec.session_switch_profile:
                    # /profiles: Enter раскрывает пикер (спека §12.1, ревизия).
                    self._open_profiles_picker(cmd.name)
                    return
                # Toggle-строка: Enter значение не трогает — «готово»: закрыть
                # палитру и очистить поле, как Esc. Меняют значение ←/→.
                self._close_palette()
                inp.text = ""
                return
            typed = event.value.strip()
            has_args = len(typed.split()) > 1
            if cmd is not None and not has_args:
                if not cmd.runs_on_enter:
                    inp.text = f"/{cmd.name} "
                    inp.move_cursor(inp.document.end)
                    self._close_palette()
                    inp.focus()
                    return
                self._close_palette()
                inp.text = ""
                await self._handle_command(cmd.name)
                return
            self._close_palette()
            # Свободный ввод с / (например, /view path/to/file) — общим путём
            # ниже: там решается, команда это или ответ на вопрос агента.
            if not typed.startswith("/"):
                inp.text = ""
                return

        text = event.value.strip()
        was_blank = inp.is_blank()
        inp.text = ""
        if not text:
            # Enter в пустом поле не внизу ленты — к низу (не отправка).
            if was_blank and not self._stick_bottom:
                self._scroll_to_bottom()
            return

        # Команда или текст агенту — решается до ветки вопроса: она снимает
        # вопрос и при пропавшем владельце пропускает ответ дальше.
        is_command = self._is_command_input(text)
        if is_command and text.startswith("/"):
            await self._handle_command(text[1:])
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
                    self._mount_nested_user(
                        parent_call_id=child_pid, prose=text, images=images
                    )
                    parent_row = self._active_calls.get(child_pid)
                    if parent_row is not None:
                        self._top_blocks().resumed(parent_row)
                else:
                    client_id = self._track_pending(
                        display_text=text, wire_content=text, images=images
                    )
                    self._mount_user_reply(
                        text,
                        state="pending",
                        client_id=client_id,
                        images=images,
                    )
                # Ответ с картинками — объектом HumanAnswer (дизайн картинок §20.3);
                # текст — строкой, как раньше: чужие порты SDK ждут str.
                if images:
                    from gc_core.tools.turn.ask_human import HumanAnswer

                    target.submit_ask_human_reply(
                        HumanAnswer(text=text, attachments=images)
                    )
                else:
                    target.submit_ask_human_reply(text)
                # Optimistic: снимаем баннер сразу после ответа, не ждём
                # function_call_output в полле (иначе 5–10с «висящего» вопроса
                # и hide индикатора, пока агент уже работает). Cleanup B-2
                # в _render_event остаётся страховкой для replay/cancel.
                self._unmount_ask_human_banner()
                self._reset_thinking_timer()
                self._on_user_text_submitted(text, images)
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

        if is_command and text.startswith("!"):
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
            full_query, patch = await self._consume_pending_prefix(text)
            display_query, skill_loaded = self._skill_display_args(text)
            technical = self._build_attachment_technical(text)
            client_id = self._track_pending(
                display_text=text, wire_content=full_query, images=images
            )
            self._mount_user_reply(
                text, state="pending", client_id=client_id, images=images
            )
            self._prepare_for_new_run()
            self._reset_thinking_timer()
            self._on_user_text_submitted(text, images)
            runner.start(
                query=full_query,
                thread_id=tid,
                system=self.app._system,
                message_id=msg_id,
                technical=technical,
                display_query=display_query,
                skill_loaded=skill_loaded,
                attachments=images,
            )
            if patch:
                self._write_tui_meta(tid, patch)
            branch = current_branch(self._current_cwd())
            if branch:
                self._write_tui_meta(tid, {"branch": branch})
            # Абсолютный путь проекта на момент создания сессии (исторический,
            # не живой cwd). Безусловно — путь есть даже в не-git папке.
            try:
                ppath = str(Path(self._current_cwd()).resolve())
            except Exception:
                ppath = ""
            if ppath:
                self._write_tui_meta(tid, {"project_path": ppath})
            return

        if runner.is_running():
            self._on_user_text_submitted(text, images)
            message = {"role": "user", "content": text, "id": msg_id}
            if images:
                # В тред картинки копирует ядро при приёме реплики (дизайн картинок §3).
                message["attachments"] = [att.to_dict() for att in images]
            self._queue_user_message(
                message,
                display_text=text,
                wire=text,
                images=images,
                technical=self._build_attachment_technical(text),
            )
            return

        # Задача есть, но run завершился (или открыта через /sessions) — продолжаем тот же
        # thread, применяя отложенные skill/cwd к этому сообщению.
        full_query, patch = await self._consume_pending_prefix(text)
        display_query, skill_loaded = self._skill_display_args(text)
        if patch and self._store is not None:
            self._write_tui_meta(self.app.tui_state.active_task_id, patch)
        client_id = self._track_pending(
            display_text=text, wire_content=full_query, images=images
        )
        self._mount_user_reply(
            text, state="pending", client_id=client_id, images=images
        )
        self._prepare_for_new_run()
        self._reset_thinking_timer()
        self._on_user_text_submitted(text, images)
        technical = self._build_attachment_technical(text)
        runner.start(
            query=full_query,
            thread_id=self.app.tui_state.active_task_id,
            system=self.app._system,
            message_id=msg_id,
            technical=technical,
            display_query=display_query,
            skill_loaded=skill_loaded,
            attachments=images,
        )

    def on_command_text_area_paste_collapsed(
        self, event: CommandTextArea.PasteCollapsed
    ) -> None:
        self._show_footer_hint(PASTE_HINT_TEXT, seconds=5.0)

    def on_command_text_area_image_paste_requested(
        self, event: CommandTextArea.ImagePasteRequested
    ) -> None:
        # Без on_miss вставка просила именно изображение (пустая вставка) — отказы.
        self._start_image_paste(
            path=event.path,
            on_miss=event.on_miss,
            strict=event.on_miss is None,
            files_only=event.files_only,
        )

    def _start_image_paste(
        self,
        *,
        path: Optional[str],
        on_miss: Optional[_OnMiss],
        strict: bool,
        files_only: bool = False,
    ) -> None:
        """Запустить вставку изображения (спека §12): воркером, «Вставка…» до результата.
        Путь, вставленный во время чтения, ждёт своей очереди: терминал присылает каждый
        файл отдельной вставкой (Cmd+V после копирования в Finder, перетаскивание
        нескольких файлов). `strict` — жест просил изображение (Alt+V, пустая вставка):
        полный лимит и нечем читать буфер — отказы, у прочих — «изображения нет»."""
        if self._image_paste_busy:
            if (path is not None or files_only) and on_miss is not None:
                # Путь или Ctrl+V с текстом — в очередь, по порядку: не теряется
                # (жест «вставьте ещё раз — развернуть»), не обгоняет идущую вставку и
                # проходит свою проверку файлов (имена файлов Finder текстом не
                # проскочат).
                self._image_paste_queue.append((path, on_miss, files_only))
            else:
                self._show_footer_refusal(IMAGE_PASTE_WAIT_HINT)
            return
        self._image_paste_busy = True
        self._show_footer_hint(
            IMAGE_PASTE_BUSY_HINT, seconds=_IMAGE_PASTE_HINT_SECONDS
        )
        self._image_paste_worker = self.run_worker(
            self._image_paste(
                path=path,
                on_miss=on_miss,
                strict=strict,
                files_only=files_only,
            ),
            name="image-paste",
            group="image-paste",
            exit_on_error=False,
        )

    async def _image_paste(
        self,
        *,
        path: Optional[str],
        on_miss: Optional[_OnMiss],
        strict: bool,
        files_only: bool,
    ) -> None:
        """Воркер вставки: эта вставка, затем очередь путей по порядку. Флаг «идёт
        вставка», очередь и своя подсказка снимаются при любом исходе, включая отмену
        двойным Esc (она отменяет и очередь)."""
        try:
            inserted = await self._image_paste_one(
                path=path,
                on_miss=on_miss,
                strict=strict,
                files_only=files_only,
            )
            while self._image_paste_queue:
                queued_path, queued_on_miss, queued_files_only = (
                    self._image_paste_queue.pop(0)
                )
                inserted |= await self._image_paste_one(
                    path=queued_path,
                    on_miss=queued_on_miss,
                    strict=False,
                    files_only=queued_files_only,
                )
        finally:
            self._image_paste_busy = False
            self._image_paste_queue.clear()
            self._end_image_paste_hint()
        if inserted:
            self._after_image_inserted()

    async def _image_paste_one(
        self,
        *,
        path: Optional[str],
        on_miss: Optional[_OnMiss],
        strict: bool,
        files_only: bool = False,
    ) -> bool:
        """Одна вставка: источник, лимит и карман — `image_paste.astage_paste`, здесь —
        отказ текстом, плашки или «изображения нет». Сбой — в лог, приложение не падает.
        True — в поле встали плашки."""
        try:
            inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
            limit = read_images_config(
                self.app.effective_settings()
            ).max_per_message
            outcome = await image_paste.astage_paste(
                path,
                self.app.image_store,
                room=limit - len(inp.images_in_buffer()),
                strict=strict,
                files_only=files_only,
            )
            if isinstance(outcome, image_paste.PasteRefusal):
                # Жест без заказа картинки (путь, Ctrl+V): не влезла — ещё и запасная
                # вставка, как при «картинки нет»; отказ объясняет, почему не плашка.
                # Отказ — после вставки: правка поля гасит отказ.
                if (
                    outcome.reason == image_paste.REFUSE_LIMIT
                    and not strict
                    and on_miss
                ):
                    missed = on_miss()
                    if inspect.isawaitable(missed):
                        await missed
                self._show_footer_refusal(
                    IMAGE_PASTE_REFUSALS[outcome.reason].format(n=limit)
                )
            elif outcome:
                # Номер — после чтения: пока оно шло, могла смениться сессия.
                first = self._next_image_number()
                used = inp.insert_image_chips(outcome, first)
                if used:
                    self._image_seq_issued[self._active_task_id] = (
                        first + used - 1
                    )
                return True
            elif on_miss is None:
                self._show_footer_refusal(IMAGE_REFUSAL_EMPTY_CLIPBOARD)
            else:
                missed = on_miss()
                if inspect.isawaitable(missed):
                    await missed
        except Exception:
            _log.warning("image paste failed", exc_info=True)
        return False

    def _after_image_inserted(self) -> None:
        """Вставка поставила плашки: о буфере, из которого они могли прийти, совет больше
        не звучит; раз за запуск — как открыть вставленное. Звучит после всей очереди
        путей: плашка следующего пути погасила бы его сразу."""
        self._clipboard_tip_stamp = self._clipboard_stamp
        if not self._image_open_tip_shown and self._footer_tip_fits():
            self._image_open_tip_shown = True
            self._show_footer_tip(IMAGE_OPEN_TIP)

    def _on_terminal_focus(self, focused: bool) -> None:
        if focused:
            self._check_clipboard_tip()
            self._refresh_trace_overlays()

    def _check_clipboard_tip(self) -> None:
        """Проверить буфер обмена вне UI-потока (на macOS — процесс osascript)."""
        self.run_worker(
            self._aclipboard_tip(),
            name="clipboard-tip",
            group="clipboard-tip",
            exclusive=True,
            exit_on_error=False,
        )

    async def _aclipboard_tip(self) -> None:
        """Совет «в буфере изображение · клавиша — вставить» — один раз на содержимое
        буфера. Не прозвучал (футер занят, сверху диалог) — прозвучит при следующем
        возвращении в окно."""
        found = await asyncio.to_thread(image_hints.read_clipboard_tip)
        if found is None:
            return
        stamp, tip = found
        self._clipboard_stamp = stamp
        if stamp == self._clipboard_tip_stamp or not self._footer_tip_fits():
            return
        self._clipboard_tip_stamp = stamp
        self._show_footer_tip(tip)

    def _cancel_image_paste(self) -> None:
        """Двойной Esc во время вставки: воркер отменяется вместе с очередью путей (флаг,
        очередь и подсказку снимает его finally); чтение ОС в потоке дорабатывает впустую."""
        if self._image_paste_worker is not None:
            self._image_paste_worker.cancel()

    def _next_image_number(self) -> int:
        """Номер следующей image-плашки (спека §4): сквозной по треду — после
        `runtime.image_seq` из meta активного треда и после последнего номера, выданного в
        этом процессе (сообщение с картинкой могло ещё не дойти до meta)."""
        tid = self._active_task_id
        meta = (
            self._store.get_task_meta(tid)
            if tid and self._store is not None
            else None
        )
        return (
            max(read_image_seq(meta), self._image_seq_issued.get(tid, 0)) + 1
        )

    def _end_image_paste_hint(self) -> None:
        """Конец вставки: снять только свою подсказку («Вставка…», «дождитесь…», «Esc ещё
        раз — отменить вставку»); отказ, «вставьте ещё раз…» и прочие чужие живут по
        своему канону."""
        if (
            self._footer_hint_active
            and self._footer_hint_text in IMAGE_PASTE_OWN_HINTS
        ):
            self._clear_footer_hint()

    async def _apaste_os_text_or_refuse(self, inp: CommandTextArea) -> None:
        """Alt+V без картинки: текст из буфера ОС (вне UI-потока — на Linux это до трёх
        процессов); нет и его — отказ (контракт §2)."""
        text = await asyncio.to_thread(os_ops.read_clipboard)
        if text:
            inp._handle_paste(text)
        else:
            self._show_footer_refusal(IMAGE_REFUSAL_EMPTY_CLIPBOARD)

    async def _consume_pending_prefix(self, text: str) -> tuple[str, dict]:
        """Встроить отложенный skill в сообщение; вернуть (query, meta-patch).

        Сбрасывает pending_skill_id. Используется и при auto-create,
        и при продолжении существующего thread'а.
        """
        pending_skill = self.app.tui_state.pending_skill_id
        self.app.tui_state.pending_skill_id = None
        prefix: list[str] = []
        patch: dict = {}
        if pending_skill:
            from gc_tui.services.skills_scan import skill_delivery_off_loop

            delivery, loaded = await skill_delivery_off_loop(
                self.app.base_settings(), pending_skill
            )
            # Не загружен — сообщение уходит без него (спека P3 §2.3).
            if loaded:
                prefix.append(delivery)
                patch["skills"] = [pending_skill]
            self._pending_skill_marker = (pending_skill, loaded)
        return ("\n".join(prefix + [text]) if prefix else text), patch

    def _skill_display_args(
        self, clean_text: str
    ) -> tuple[str | None, str | None]:
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
        from gc_core.message_policy.user_message_policy import (
            wrap_technical_user_content,
        )

        from gc_tui.services.file_index import (
            build_attachment_footer,
            extract_at_mentions,
            resolve_attachment,
        )

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
        from gc_tui.screens.settings import SettingsScreen

        self.app.reload_config_if_changed()  # свежесть глобала — только на пути Settings (дизайн 2026-07-21 §4)
        self.app.push_screen(
            SettingsScreen(
                settings=self.app.base_settings(),
                config_path=getattr(self.app, "_config_path", None),
                on_apply=self.app.reload_config,
            )
        )

    def _cmd_config(self) -> None:
        from gc_tui.screens.config import ConfigScreen

        ref = self.app.active_profile_ref()
        profile_source = None
        profile_path = None
        if ref is not None:
            profile_source = (
                "проектный" if ref.root_kind == "project" else "глобальный"
            )
            profile_path = str(ref.path)
        self.app.push_screen(
            ConfigScreen(
                settings=self.app.effective_settings(),
                config_path=getattr(self.app, "_config_path", None),
                project_local=getattr(self.app, "_config_project_local", None),
                profile_source=profile_source,
                profile_path=profile_path,
                permission_mode=self.app.effective_permission_mode(),
                permission_mode_source=self.app.permission_mode_source(),
            )
        )

    def _cmd_view(self, arg: str) -> None:
        """`/view <path>` → FileViewerScreen (ошибки тоже на этом экране)."""
        from gc_tui.screens.file_view import FileViewerScreen
        from gc_tui.services.file_view import FileViewSettings

        file_view = self.app.base_settings().settings.tui.file_view
        store = getattr(self.app, "view_positions", None)
        if store is None:
            from gc_tui.services.file_view import ViewPositionStore

            store = ViewPositionStore()
            self.app.view_positions = store
        self.app.push_screen(
            FileViewerScreen(
                raw_path=arg or "",
                cwd=self._current_cwd(),
                store=store,
                settings=FileViewSettings.from_mapping(file_view.model_dump()),
            )
        )

    def _cmd_mcp(self) -> None:
        from gc_tui.screens.mcp import McpScreen

        self.app.push_screen(
            McpScreen(
                settings=self.app.base_settings(),
                tools_snapshot=self._mcp_screen_snapshot(),
            )
        )

    def _cmd_diff(self, *, source: Optional[str] = None) -> None:
        """Экран изменений. `source` задан — открыть сразу в нём (клик по сводке)."""
        from gc_tui.screens.diff import DiffScreen
        from gc_tui.services.edits_view import SOURCE_SESSION

        cfg = self.app.base_settings()
        launch_root = cfg.runtime.launch_root
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
                settings=cfg,
                on_undo_feedback=self._on_diff_undo_feedback,
                on_ui_ack=self._on_diff_ui_ack,
                threads_dir=threads_dir,
                system_id=system_id,
                thread_id=thread_id,
                source=source or SOURCE_SESSION,
            )
        )

    def on_open_edits_screen_request(self, message) -> None:
        """Клик по сводке правок в строке-итоге → экран в источнике сводки.

        Запрос исполняется, только пока трейс на вершине стека. Сообщение
        всплывает несколько тактов, и за это время поверх него мог встать
        экран — в том числе открытый этим же запросом: без проверки быстрая
        серия жестов клала бы экраны стопкой, и первый Esc «не закрывал» их.
        """
        if self.app.screen is not self:
            return
        self._cmd_diff(source=message.source)

    def _on_diff_ui_ack(self, text: str) -> None:
        """TUI-only строка в трейсе после отката / git add (не в thread модели)."""
        if not text:
            return
        self._mount_trace_row(
            prefix="",
            content=text,
            kind_class="line-meta",
            focusable=False,
        )

    def _on_diff_undo_feedback(self, restored_keys, deleted_keys=()) -> None:
        """T15: после undo — technical user message агенту (визуал — через on_ui_ack)."""
        from gc_core.agent_edits.feedback import build_undo_feedback_message

        msg = build_undo_feedback_message(
            restored_keys=restored_keys,
            deleted_keys=deleted_keys,
        )
        if msg is None:
            return
        self._append_technical_to_thread(msg)

    def _projection_from_app_config(self):
        """``(tags, strip_think)`` из живого конфига сессии (профиль + overrides)."""
        from gc_core.config_modules.strip_tags_projection import (
            projection_tags_from_config,
        )

        return (
            projection_tags_from_config(self.app.effective_settings()),
            True,
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
        from gc_core.thread_store import (
            append_new_thread_messages,
            load_thread_messages,
            load_thread_meta,
            save_thread_meta,
        )

        tags, strip_think = self._projection_from_app_config()
        _msgs, keys, max_seq = load_thread_messages(threads_dir, system, tid)
        written = append_new_thread_messages(
            threads_dir,
            system,
            tid,
            [msg],
            keys,
            start_seq=max_seq,
            tags=tags,
            strip_think=strip_think,
        )
        if written and self._store is not None:
            meta = load_thread_meta(threads_dir, system, tid) or {}
            try:
                meta["messages_count"] = (
                    int(meta.get("messages_count", 0)) + written
                )
            except (TypeError, ValueError):
                meta["messages_count"] = written
            save_thread_meta(threads_dir, system, tid, meta)

    def _cmd_new(self) -> None:
        self.request_thread_switch(None)

    def request_thread_switch(self, target: str | None) -> None:
        """Единственный вход смены открытого треда (решение 25): `target` —
        тред, None — новая пустая сессия.

        Идёт ход — вопрос «остановить и перейти»; остаться — ничего не
        меняется. Ход не идёт — переход сразу; кончившийся, но не
        закрытый тиком ход сначала закрывается (исход — в его тред)."""
        if target is not None and target == self._active_task_id:
            self._open_thread(target)
            return
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and runner.is_running():
            from gc_tui.screens.confirm import ConfirmScreen

            def _on_result(confirmed: bool) -> None:
                if confirmed:
                    self._stop_run_then_switch(target)

            # Вопрос — из очереди этого экрана: ответ Textual доставляет тому,
            # кто вопрос открыл, а вход зовут и с других экранов (список
            # сессий закрывает себя до вопроса — ответ ушёл бы в закрытый
            # экран и пропал).
            self.call_later(
                self.app.push_screen,
                ConfirmScreen(
                    title=RUN_SWITCH_TITLE,
                    message=RUN_SWITCH_MESSAGE,
                    hint=RUN_SWITCH_HINT,
                ),
                _on_result,
            )
            return
        self._settle_ended_run()
        self._open_thread(target)

    def _stop_run_then_switch(self, target: str | None) -> None:
        """Обычный стоп (очередь — назад в поле), вопросы хода сняты, исход
        «stopped» — в тред хода сразу; затем переход. Поток хода
        дорабатывает в фоне, отвязанный от окна, как после обычного стопа:
        всё, что он ещё пишет, идёт в его тред."""
        if self._confirm_active:
            self._hide_confirm_banner()
        if self.app.tui_state.ask_human_state is not None:
            self._unmount_ask_human_banner()
        self._stop_and_replace_runner()
        self._settle_ended_run()
        self._open_thread(target)

    def _open_thread(self, target: str | None) -> None:
        """Сменить открытый тред; ход не идёт (проверил вход)."""
        if target is not None:
            self.attach_active_task(target, is_resume=True)
            return
        self._resumed_prompt = None  # новая сессия — без resume-промпта
        self._last_terminal_title = format_terminal_title()
        set_terminal_title(self._last_terminal_title)
        # В прежнем run executor мог оставить in-memory usage-метрики
        # (футер берёт их через runner.usage_snapshot()). Для новой сессии
        # нужен clean executor — иначе футер показывает токены/стоимость
        # прошлой сессии.
        self._stop_and_replace_runner()
        self._switch_to_empty_state()

    def _request_exit(self) -> None:
        """Модалка подтверждения выхода (Esc/Ctrl+C вхолостую)."""
        from gc_tui.screens.confirm import ConfirmScreen

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
                self._stop_and_replace_runner()
                self._show_stop_state()
            else:
                # /init (or no live runner): parked worker now unblocked → cancel/deny
                self._mount_trace_row(
                    prefix="· ",
                    content="confirm отменён",
                    kind_class="line-meta",
                )
            return

        if runner is None or not runner.is_running():
            self._mount_trace_row(
                prefix="· ",
                content="нет активного запуска",
                kind_class="line-meta",
            )
            return

        # Cancel-with-undo (агент не успел ничего выдать + input пустой) — отдельно.
        if self._can_cancel_with_undo():
            self._cancel_with_undo()
            return

        # Обычный стоп: пока поток жив — мигающий «■» в баре, исход —
        # строкой «■ Отменено» в ленте (S10 §4.4), без [stopped]-строки.
        self._stop_and_replace_runner()
        self._show_stop_state()

    def _stopping_alive(self) -> bool:
        """Остановленный ход ещё дорабатывает (стоп кооперативный)."""
        runner = self._stopping_runner
        if runner is not None and not runner.is_running():
            self._stopping_runner = runner = None
        return runner is not None

    def _show_stop_state(self) -> None:
        """Стоп хода (решение 26, S10 §4.4): поток дорабатывает — в баре
        один мигающий `■`; уже вышел — строка итога стопа (из записи, без
        неё — запасная «■ Отменено») и уход бара в этом же вызове."""
        if self._stopping_alive():
            self._show_thinking_indicator(state="stopping")
            return
        self._settle_ended_run()
        # Работы нет: индикатор и бар уходят и без застанного тиком прогона.
        self._destroy_thinking_indicator()

    def _can_cancel_with_undo(self) -> bool:
        """Откат отправки: агент ничего не выдал, поле пустое, очереди нет.
        «Ничего не выдал» решает ядро (`produced_output` раннера: дельта
        стрима, запись агента, старт тула) — хвост ленты отстаёт от него.
        Прогон без отправки этого вида (план утверждён, тред сменён) —
        откатывать нечего.
        С очередью откатывать нечего: последняя отправка — в очереди, её
        с остальными вернёт в поле стоп (`_stop_and_replace_runner`). Нет и
        реплик, которые ядро держит после прошлого стопа или конца хода:
        они у агента, откатывать их нельзя."""
        input_text = ""
        try:
            input_text = self.query_one(_SEL_CMDINPUT).text
        except Exception:
            pass
        runner = getattr(self.app, "active_runner", None)
        return (
            self._last_user_message_id is not None
            and runner is not None
            # Зафиксированный прогон (ошибка при живом потоке) уже закрыт.
            and not self._run_settled(runner)
            and not runner.produced_output()
            and not input_text
            and not self._queued_tail(
                self.query_one(_SEL_TRACE, VerticalScroll)
            )
            and not self._has_held_replies()
        )

    def _stop_and_replace_runner(self) -> None:
        """Кооперативный stop + mark + обнуление active_runner.

        Следующий self.app.get_runner() создаст свежий — clean restart для
        нового user-запроса. Старый runner может ещё досидеть шаг в фоне,
        отвязанный от окна (`detach_run_sinks`): его ответ в тред не уйдёт
        (discard_writes_after_stop), служебные записи — только в его тред,
        стрим, прогресс и предохранитель окна — уже не его.
        """
        runner = self.app.active_runner
        withdrawn = []
        if runner is not None:
            # До стопа: снять очередь под замком ядра. Впущенное остаётся
            # репликой и сверится по треду — его текст в поле не идёт.
            withdrawn = self._withdraw_queue(runner)
            # Стоп ЖИВОГО прогона → терминальный статус «stopped». Флаг app-уровня
            # (не runner.stop_requested): ниже active_runner обнуляется, и
            # спадающий фронт в _poll_active уже не увидит этот runner.
            if runner.is_running():
                self.app.last_stop_requested = True
                self._stopping_runner = runner
                # Предохранитель хода — в его тред сейчас: поток доработает
                # отвязанным, и общий объект к тому времени может держать
                # уже другой тред.
                self.app.persist_circuit_breaker(
                    self.app.run_thread_id(runner)
                )
            runner.stop()
            runner._marked_stopped = True
            # Поток может ещё досиживать HTTP — тогда логгер отдаст он сам.
            release = getattr(runner, "release", None)
            if callable(release):
                release()
        self.app.active_runner = None
        self.app.detach_run_sinks()
        self._return_queue_to_input(withdrawn)

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
        self._stop_and_replace_runner()
        self._show_stop_state()

    def _cancel_with_undo(self) -> None:
        """Откатить последний user-send: убрать user-widget, восстановить input.

        Условие срабатывания — `_can_cancel_with_undo`: агент ещё ничего не
        выдал (флаг ядра), input пуст.

        Реплика к этому моменту уже могла быть записана в тред (persist user-
        сообщения происходит до ответа бэкенда). Поэтому, помимо визуального
        отката, ставим заявку на удаление этой записи по id — она исполнится в
        poll, когда тред станет idle (см. _run_orphan_cleanups): без гонок с
        дозаписью worker'а.
        """
        from gc_tui.widgets.user_reply import UserReplyContainer

        # Заявка на удаление осиротевшей реплики (по id, когда тред idle).
        old_runner = getattr(self.app, "active_runner", None)
        tid = self.app.tui_state.active_task_id
        msg_id = self._last_user_message_id
        if tid is not None and msg_id and self._store is not None:
            self._pending_orphan_cleanups.append((old_runner, tid, msg_id))
            self._undone_reply_ids.setdefault(tid, set()).add(msg_id)

        # Удалить последний UserReplyContainer
        # В поле возвращается снимаемая реплика, её `plain_text` (скилл
        # первым словом) и картинки — тот же источник, что у ↑ и стопа.
        user_widgets = list(self.query(UserReplyContainer))
        restored = user_widgets[-1] if user_widgets else None
        if restored is not None:
            restored.remove()

        # Полный откат: убрать pending-запись из meta (иначе на re-entry
        # _mount_failed_from_meta покажет её как фантомную [!] failed-реплику).
        cid = self._last_pending_client_id
        if cid is not None:
            self._remove_pending_record(cid)
            self._pending_user_widgets.pop(cid, None)
            self._last_pending_client_id = None

        if restored is not None:
            self._restore_input(restored.plain_text, restored.images)

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
        self._close_undone_run(old_runner, msg_id)

    def _close_undone_run(self, runner, msg_id: str | None) -> None:
        """Откатанный прогон зафиксирован без исхода: фронты его не видят,
        строки и записи меты от него не будет. Прошлый ход снова последний
        — его строка свежая, мета открытого треда — как до отправки (§3.8).
        """
        if runner is not None:
            self._settled_run = (runner, getattr(runner, "run_seq", None))
        self._stopping_runner = None
        self._run_pending = False
        self._was_running = False
        self._watched_runner = None
        self._watched_run_seq = None
        self._outcome_superseded = False
        # Строка снятой реплики снята в этом же обработчике и ещё в `query`.
        lines = [
            w
            for w in self.query(TurnLine)
            if w.opened_by != msg_id and not textual_compat.is_being_removed(w)
        ]
        if lines:
            lines[-1].freshen(lines[-1].summary, lines[-1].error)
        snapshot, self._pre_run_tui_meta = self._pre_run_tui_meta, None
        if snapshot is not None and snapshot[0] == self._active_task_id:
            self._write_tui_meta(*snapshot)
        self._sync_indicator_bar()

    def _restore_input(
        self, text: str, images: tuple[Attachment, ...] = ()
    ) -> None:
        """Вернуть отправленное в поле (откат отправки, ↑ из очереди):
        курсор в конец, чтобы сразу продолжить печатать."""
        try:
            input_field = self.query_one(_SEL_CMDINPUT)
            input_field.text = text
            # Плашки картинок возвращаются вместе с текстом: подписи уже в нём,
            # записи стора сброшены отправкой (дизайн картинок §12).
            input_field.restore_images(images)
            input_field.cursor_location = input_field.document.end
        except Exception:
            pass

    def _discard_orphan_and_resync(self, tid: str, msg_id: str) -> bool:
        """Удалить запись реплики по id (с записью итога её хода и строкой
        хода в ленте) и пересинхронизировать курсор трейса.

        Без ресинка poll увидит ужавшийся data_*.jsonl как «file truncated» →
        stream_reset → полная перерисовка трейса (моргание). Курсор
        переносится в переписанный файл на то же прочитанное: тик ничего не
        перерисовывает (виджет уже снят точечно) и дочитывает то, что легло
        после курсора, — ответ и запись итога нового хода не теряются."""
        if self._store is None:
            return False
        own = tid == self._active_task_id
        try:
            tags, strip_think = self._projection_from_app_config()
            removed, cursor = self._store.discard_message_by_id(
                tid,
                msg_id,
                tags=tags,
                strip_think=strip_think,
                cursor=self._cursor if own else None,
            )
        except Exception:
            return False
        if removed and own:
            self._cursor = cursor
            self._drop_turn_lines(msg_id)
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
        # Остановленный ход тоже пишет файл треда, пока его поток жив.
        stopping = self._stopping_runner
        if stopping is not None and stopping.is_running():
            active_running = True
        still_pending: list[tuple] = []
        for old_runner, tid, msg_id in self._pending_orphan_cleanups:
            old_busy = old_runner is not None and old_runner.is_running()
            thread_busy = active_running and active_tid == tid
            if old_busy or thread_busy:
                still_pending.append((old_runner, tid, msg_id))
                continue
            self._discard_orphan_and_resync(tid, msg_id)
            self._forget_undone_reply(tid, msg_id)
        self._pending_orphan_cleanups = still_pending

    def _forget_undone_reply(self, tid: str, msg_id: str) -> None:
        """Чистка сироты прошла: записи её хода в файле больше нет."""
        undone = self._undone_reply_ids.get(tid)
        if undone is None:
            return
        undone.discard(msg_id)
        if not undone:
            del self._undone_reply_ids[tid]

    def _reset_history_navigation(self) -> None:
        """Выход из режима history-навигации. Зовётся при правке input или send."""
        self._history_index = None
        self._history_draft = None
        self._history_last_programmatic_text = None

    def _history_step(self, step) -> tuple[int, str] | None:
        """Шаг по истории мимо записей, равных тексту поля или исходному
        тексту навигации (решение 28): такой шаг на экране ничего не меняет
        — например, реплика, снятая ↑ из очереди, уже лежит в истории."""
        try:
            current = self.query_one(_SEL_CMDINPUT).text
        except Exception:
            current = ""
        same = {current, self._history_draft or ""}
        result = step(self._history_index)
        while result is not None and result[1] in same:
            result = step(result[0])
        return result

    def _history_prev(self) -> None:
        """Шаг назад по истории. На первый вызов — сохранить draft."""
        if self._history is None or self._history.size == 0:
            return
        if self._history_index is None:
            try:
                input_field = self.query_one(_SEL_CMDINPUT)
                self._history_draft = input_field.text
            except Exception:
                self._history_draft = ""
            self._history_index = self._history.size
        result = self._history_step(self._history.prev)
        if result is None:
            return
        new_index, text = result
        self._history_index = new_index
        self._history_last_programmatic_text = text
        try:
            input_field = self.query_one(_SEL_CMDINPUT)
            input_field.text = text
            input_field.cursor_location = input_field.document.end
        except Exception:
            pass

    def _history_next(self) -> None:
        """Шаг вперёд по истории. За newest — вернуть draft."""
        if self._history is None or self._history_index is None:
            return
        result = self._history_step(self._history.next)
        if result is None:
            draft_text = self._history_draft or ""
            # Возврат к draft: технически это «выход» из навигации,
            # но Changed для draft-текста придёт ПОСЛЕ обнуления state
            # → ничего не нужно ловить, элиф ниже его игнорирует.
            try:
                input_field = self.query_one(_SEL_CMDINPUT)
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
            input_field = self.query_one(_SEL_CMDINPUT)
            input_field.text = text
            input_field.cursor_location = input_field.document.end
        except Exception:
            pass

    def _resolve_cached_skill(self, name: str) -> ResolveResult | None:
        """Точный разбор имени скилла (полный id, пак, короткое имя) по
        прогретому кэшу, без нечёткого поиска; холодный кэш — None."""
        if self._skills_cache is None:
            return None
        catalog = catalog_from_entries(self._skills_cache)
        return resolve_skill_exact(name, catalog)

    def _insert_palette_skill(self, inp: TextArea, typed: str) -> bool:
        """Enter на строке скилла палитры: вставить `/имя ` — текст
        дописывают и отправляют следующим Enter (спека P5 §3.2)."""
        skill = self._palette.selected_skill()
        if skill is None or " " in typed.strip():
            return False
        inp.text = (
            f"/{skill.get('input_name') or self._skill_input_name(skill)} "
        )
        inp.move_cursor(inp.document.end)
        self._close_palette()
        inp.focus()
        return True

    def _palette_skills(self) -> list[dict]:
        """Скиллы для палитры: запись + запускаемое имя (`input_name`) —
        то же, что вставит Enter."""
        return [
            {**s, "input_name": self._skill_input_name(s)}
            for s in self._skills_cache or []
        ]

    def _skill_input_name(self, skill: dict) -> str:
        """Запускаемый вид скилла (`skill_input_name`) по прогретому кэшу
        и реестру команд."""
        catalog = (
            None
            if self._skills_cache is None
            else catalog_from_entries(self._skills_cache)
        )
        return skill_input_name(
            str(skill.get("id", "")),
            catalog,
            is_command=lambda name: self._registry.lookup(name) is not None,
        )

    async def _cmd_skill(self, arg: str) -> None:
        """`/skill <имя> [текст]`: имя — полный id или короткое (точный
        разбор); с текстом — немедленная пара, без — pending/running путь."""
        if self._skills_cache is None:
            self._skills_cache = await self._load_skills_safe()
        first, _, rest = arg.partition(" ")
        res = self._resolve_cached_skill(first)
        if isinstance(res, Resolved):
            await self._cmd_use_skill(res.canonical_id, prompt=rest.strip())
        elif isinstance(res, Failed) and res.reason == "ambiguous":
            self._render_skill_ambiguous(first, res.suggestions)
        else:
            # Первый токен — не скилл каталога (id скиллов без пробелов):
            # текст уходит без скилла, без текста — только ошибка.
            await self._skill_missing(first, rest.strip())

    async def _dispatch_skill_by_name(self, name: str, arg: str) -> None:
        """`/имя текст` без такой команды ≡ `/skill имя текст` — только при
        точном совпадении по прогретому кэшу. Опечатка скилл не запускает:
        «неизвестная команда» с кандидатами — команда, затем скиллы.
        Холодный кэш — «неизвестная команда» (без ожидания прогрева)."""
        res = self._resolve_cached_skill(name)
        if isinstance(res, Resolved):
            await self._cmd_use_skill(res.canonical_id, prompt=arg)
        elif isinstance(res, Failed) and res.reason == "ambiguous":
            self._render_skill_ambiguous(name, res.suggestions)
        else:
            self._render_command_error(name, self._command_candidates(name))

    def _command_candidates(self, name: str) -> list[str]:
        """«Вы имели в виду»: ближайшая команда, затем нечёткие скиллы."""
        found = closest_command(name, self._registry)
        out = [found] if found else []
        for sid in self._fuzzy_skill_ids(name):
            runnable = "/" + self._skill_input_name({"id": sid})
            if runnable[1:] not in out:
                out.append(runnable[1:])
        return out

    def _fuzzy_skill_ids(self, name: str) -> list[str]:
        """Полные id скиллов, близких к `name`: короткое неоднозначное
        имя раскрывается во все свои id."""
        if not self._skills_cache:
            return []
        catalog = catalog_from_entries(self._skills_cache)
        ids: list[str] = []
        for cand in suggest_skills(name, catalog, 3):
            res = resolve_skill_exact(cand, catalog)
            found = (
                [res.canonical_id]
                if isinstance(res, Resolved)
                else list(res.suggestions)
            )
            ids += [i for i in found if i not in ids]
        return ids

    async def _cmd_use_skill(self, skill_id: str, *, prompt: str = "") -> None:
        if not skill_id:
            self._mount_trace_row(
                prefix="[!] ",
                content="/skill: укажите id навыка",
                kind_class="line-error",
            )
            return
        if prompt:
            await self._deliver_skill_with_prompt(skill_id, prompt)
            return
        runner = self.app.active_runner
        if runner is not None and runner.is_running():
            # Идёт run → подкидываем как сообщение и фиксируем в meta.
            await self._queue_skill_only(skill_id)
            return
        self._stage_skill(skill_id)

    def _stage_skill(self, skill_id: str) -> None:
        """Run не идёт (нет задачи / завершена / открыта через /sessions) —
        скилл отложенно к следующему сообщению."""
        self.app.tui_state.pending_skill_id = skill_id
        self._record_skill_meta(skill_id)
        name = self._skill_input_name({"id": skill_id})
        self._mount_trace_row(
            prefix="· ",
            content=f"скилл {name} применится к следующему сообщению",
            kind_class="line-meta",
        )

    async def _skill_missing(self, skill_id: str, prompt: str) -> None:
        """Скилла нет (спека P3 §2.3): с текстом — текст уходит обычной
        репликой, под ней `└ скилл … не найден — отправлено без него`;
        без текста — агенту ничего, только ошибка команды."""
        if prompt:
            await self._deliver_skill_with_prompt(
                skill_id, prompt, loaded=False
            )
            return
        self._render_command_error(
            self._skill_input_name({"id": skill_id}),
            [
                self._skill_input_name({"id": sid})
                for sid in self._fuzzy_skill_ids(skill_id)
            ],
            title="скилл не найден",
        )

    async def _queue_skill_only(self, skill_id: str) -> None:
        """Mid-run `/skill X` без текста — реплика из одного имени скилла
        в очереди (спека P3 §10 ответ 3). Не загрузился — как ненайденный:
        агенту ничего."""
        from gc_tui.services.skills_scan import skill_delivery_off_loop

        delivery, loaded = await skill_delivery_off_loop(
            self.app.base_settings(), skill_id
        )
        if not loaded:
            await self._skill_missing(skill_id, "")
            return
        runner = self.app.active_runner
        if runner is None or not runner.is_running():
            # Ход кончился, пока тело читалось вне цикла.
            self._stage_skill(skill_id)
            return
        msg = {
            "role": "user",
            "content": delivery,
            "id": "m_" + uuid.uuid4().hex,
            "display_content": "",
            "skill_loaded": skill_id,
        }
        self._record_skill_meta(skill_id)
        self._queue_user_message(
            msg,
            display_text="",
            wire=msg["content"],
            skill=skill_id,
        )

    def _queue_user_message(
        self,
        message: dict,
        *,
        display_text: str,
        wire: str,
        images: tuple[Attachment, ...] = (),
        skill: str | None = None,
        technical: Optional[str] = None,
    ) -> None:
        """Отправка во время хода (спека P3 §2.4) — единый путь реплики,
        `/skill X текст` и `/skill X`: сообщение (и его техспутник `@`-
        вложений с производным `id`) уходит в очередь раннера, реплика —
        в состоянии `queued` у низа ленты. Ввод субагенту реплику родителя
        не рисует. Таймер хода не сбрасывается: индикатор считает от
        начала хода."""
        from gc_core.execution.message_keys import companion_message_id

        runner = self.app.active_runner
        to_child = runner.is_child_input_active()
        runner.add_message(message)
        if technical is not None:
            runner.add_message(
                {
                    "role": "user",
                    "content": technical,
                    "id": companion_message_id(message["id"]),
                }
            )
        if to_child:
            # Реплики родителя нет — итог доставки не ждёт следующую.
            self._pending_skill_marker = None
            return
        skill_error = False
        if skill is None and self._pending_skill_marker is not None:
            # Итог доставки скилла (`/skill X текст`) — в запись очереди:
            # пересборка ленты покажет `/имя` и вернёт его в поле.
            (skill, loaded), self._pending_skill_marker = (
                self._pending_skill_marker,
                None,
            )
            skill_error = not loaded
        queued = {"message_id": message["id"]}
        if skill is not None:
            queued["skill"] = skill
        client_id = self._track_pending(
            display_text=display_text,
            wire_content=wire,
            images=images,
            queued=queued,
        )
        self._mount_user_reply(
            display_text,
            state="queued",
            client_id=client_id,
            images=images,
            skill=skill,
            skill_error=skill_error,
            message_id=message["id"],
        )
        self._refresh_queue_hint()

    async def _deliver_skill_with_prompt(
        self, skill_id: str, prompt: str, *, loaded: bool = True
    ) -> None:
        """Inline /skill <id> <prompt>: префикс «Используй skill …» встраивается в
        реплику тем же _consume_pending_prefix, что и pending-путь — единая форма
        доставки. Technical-обёртка не используется: системный промпт трактует
        technical-блоки как контекст, не команды, и слабые модели вправе их
        игнорировать. Дисплей: реплика с именем скилла и чистым промптом.

        ``loaded=False`` — скилла нет: промпт уходит без него, реплика
        несёт ошибку скилла, в meta скилл не пишется."""
        if loaded:
            self.app.tui_state.pending_skill_id = skill_id
            full_query, _ = await self._consume_pending_prefix(prompt)
        else:
            full_query, self._pending_skill_marker = prompt, (skill_id, False)
        _, skill_loaded = self._skill_display_args(prompt)
        msg_id = "m_" + uuid.uuid4().hex
        self._last_user_message_id = msg_id

        runner = self.app.active_runner
        if runner is not None and runner.is_running():
            msg = {
                "role": "user",
                "content": full_query,
                "id": msg_id,
                "display_content": prompt,
            }
            if skill_loaded:
                msg["skill_loaded"] = skill_loaded
            self._queue_user_message(
                msg,
                display_text=prompt,
                wire=full_query,
            )
            self._on_user_text_submitted(prompt)
            self._record_skill_meta(skill_loaded)
            return

        if self.app.tui_state.active_task_id is None:
            # AUTO-CREATE: зеркалим порядок auto-create в on_command_text_area_submitted.
            tid = self._begin_auto_created_task()
            runner = self.app.get_runner()
            client_id = self._track_pending(
                display_text=prompt, wire_content=full_query
            )
            self._mount_user_reply(
                prompt, state="pending", client_id=client_id
            )
            self._prepare_for_new_run()
            self._reset_thinking_timer()
            self._on_user_text_submitted(prompt)
            runner.start(
                query=full_query,
                thread_id=tid,
                system=self.app._system,
                message_id=msg_id,
                display_query=prompt,
                skill_loaded=skill_loaded,
            )
            self._record_skill_meta(skill_loaded)
            return

        # Задача есть, но run не идёт — продолжаем тот же thread.
        # Зеркалим порядок continue-thread в on_command_text_area_submitted.
        runner = self.app.get_runner()
        client_id = self._track_pending(
            display_text=prompt, wire_content=full_query
        )
        self._mount_user_reply(prompt, state="pending", client_id=client_id)
        self._prepare_for_new_run()
        self._reset_thinking_timer()
        self._on_user_text_submitted(prompt)
        runner.start(
            query=full_query,
            thread_id=self.app.tui_state.active_task_id,
            system=self.app._system,
            message_id=msg_id,
            display_query=prompt,
            skill_loaded=skill_loaded,
        )
        self._record_skill_meta(skill_loaded)

    def _record_skill_meta(self, skill_id: str | None) -> None:
        """Зафиксировать skill_id в meta.tui.skills активной задачи;
        ``None`` — скилл не загружен, писать нечего."""
        tid = self.app.tui_state.active_task_id
        if skill_id and tid is not None and self._store is not None:
            current = (self._store.get_task_meta(tid).get("tui") or {}).get(
                "skills", []
            )
            if skill_id not in current:
                self._write_tui_meta(tid, {"skills": current + [skill_id]})

    def _switch_to_empty_state(self) -> None:
        """Очистить trace и перейти в empty-state. pending_skill_id/cwd сохраняются."""
        # Сбросить терминальные error/stop-флаги app-уровня (last_runner_error и пр.):
        # иначе poll видит протухшую ошибку и репейнтит «✕ Ошибка» поверх пустой сессии.
        # Только флаги: «running» уходящему треду писать нельзя — хода в нём
        # нет, и исход потом никто не перезапишет. Остановленный ход
        # (`/new` посреди него) закрываем до ухода: тред прогона — в стор.
        self._settle_ended_run()
        self._session_lock_release()
        self._reset_run_flags()
        self._copy_seq += 1  # итог /copy прошлой сессии сюда не пишется
        self._leave_thread_view()
        self._active_task_id = None
        self.app.tui_state.active_task_id = None
        self._session_lock_hb_mono = 0.0
        self._store = None
        self._cursor = None
        self._reset_render_state()
        self._clear_trace()
        self._refresh_header()
        self._refresh_footer()
        self._sync_indicator_bar()
        # Новая сессия — номера картинок с 1: выданное прежней несозданной — не её.
        self._image_seq_issued.pop(None, None)
        self._renumber_input_images()
        self.spend_help_hint()

    def _renumber_input_images(self) -> None:
        """Смена сессии: image-плашки буфера ввода — в последовательность активного треда
        (дизайн картинок §4, §12), следом за его номерами из meta и выданными в
        процессе."""
        inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
        first = self._next_image_number()
        count = inp.renumber_image_chips(first)
        if count:
            self._image_seq_issued[self._active_task_id] = first + count - 1

    def _bar_wanted(self) -> bool:
        """Бар стоит, пока у открытого треда есть работа (S10 §3.3): прогон
        бежит и не зафиксирован, поток стопа дорабатывает, ход запущен
        отсюда и ещё не увиден тиком. `run_seq` — через `getattr`: у
        заглушек раннера его нет."""
        if self._active_task_id is None:
            return False
        if self._run_pending or self._stopping_alive():
            return True
        runner = getattr(self.app, "active_runner", None)
        if runner is None or not runner.is_running():
            return False
        if not self._runner_is_for_active_thread():
            return False
        return not self._run_settled(runner)

    def _run_settled(self, runner) -> bool:
        """Прогон раннера уже зафиксирован (`_settle_ended_run`): пара
        «раннер и `run_seq`» — у раннеров свой счёт с нуля."""
        pair = (runner, getattr(runner, "run_seq", None))
        return runner is not None and pair == self._settled_run

    def _sync_indicator_bar(self) -> None:
        """Поставить или снять бар по `_bar_wanted()`; в том же вызове —
        потолок ленты (высота бара — константа, не замер) и прижатие низа,
        когда бар ушёл. Пока стоит sticky-баннер, бар скрыт и при идущей
        работе (§4.12): над баннером один пустой ряд, как над вводом.
        Работа при этом не кончилась — придержанное не раскрывается."""
        try:
            bar = self.query_one(_SEL_INDICATOR_BAR, Vertical)
        except Exception:
            return
        was = bar.has_class(_BAR_TURN)
        wanted = self._bar_wanted()
        if not wanted and self._ui_frozen_for_select():
            # Выделение (F2): снятие бара и раскрытие — только вместе,
            # после него; догоняет тик опроса (S10 §4.14).
            return
        banner = self._sticky_banner_widget()
        shown = wanted and not (banner is not None and banner.is_mounted)
        bar.set_class(shown, _BAR_TURN)
        # Бар уходит — придержанное встаёт на его место той же
        # перерисовкой: бар −2, строка +2, низ на месте (S10 §3.4).
        revealed = not wanted and self._reveal_held()
        self._sync_trace_max_height()
        if (was and not shown) or revealed:
            self._scroll_trace_end()

    def _reveal_held(self) -> bool:
        """Раскрыть придержанное за баром; True — было что раскрыть."""
        held, self._held_reveals = self._held_reveals, []
        for reveal in held:
            reveal()
        return bool(held)

    def _handle_bang_shell(self, text: str) -> None:
        """`!command` → локальный shell в cwd TUI; результат только в трейсе."""
        from gc_tui.services.bang_shell import (
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

        bang_shell = self.app.base_settings().settings.tui.bang_shell
        settings = BangShellSettings.from_mapping(bang_shell.model_dump())
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

    async def _handle_command(self, raw: str) -> None:
        """Диспетчеризовать команду и, если она что-то дописала в трейс,
        форсировать скролл вниз. Команда — прямое действие пользователя, поэтому
        её результат показываем безусловно, даже если юзер отскроллен вверх
        (в отличие от агентских событий). Команды, открывающие экран, трейс не
        растят → счётчик не меняется → позиция чтения истории не сбивается."""
        trace = self.query_one(_SEL_TRACE, VerticalScroll)
        before = len(trace.children)
        await self._dispatch_command(raw)
        if len(trace.children) > before:
            self._stick_bottom = True  # команда юзера = re-engage
            self._scroll_trace_end()

    async def _dispatch_command(self, raw: str) -> None:
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
            await self._dispatch_skill_by_name(name, arg)
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
            await self._cmd_skill(arg)
            return
        if name == "help":
            self.app.action_help()
            return
        if name == "skills":
            from gc_tui.screens.skills import SkillsScreen

            self.app.push_screen(
                SkillsScreen(
                    skills=await self._load_skills_safe(),
                    settings=self.app.base_settings(),
                )
            )
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
        if name == "copy":
            self._cmd_copy(arg)
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
        if name == "plan":
            self._cmd_plan(arg)
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
            self._toggle_info_command(name, arg)
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
            from gc_tui.toggles import effective_toggle_names

            available = effective_toggle_names(self.app, "profiles")
            if value not in available:
                tail = (
                    f" · доступно: {', '.join(available)}" if available else ""
                )
                self._show_footer_refusal(
                    f'✗ профиль "{value}" не найден{tail}'
                )
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
            self._show_footer_refusal(f"✗ не сохранено: {exc}")

    def _toggle_info_command(self, name: str, arg: str = "") -> None:
        """`/gate` или `/answers`, набранные и подтверждённые текстом (не через
        стрелки палитры — у toggle-команд нет args-схемы, поэтому пробел после
        имени закрывает палитру и Enter доходит сюда как обычная команда):
        ничего не меняет (иначе набор команды был бы неочевидным side-effect'ом) —
        показывает текущее значение и как менять (←/→ в открытой палитре).
        Набранное значение, которого тоггл сейчас не предлагает (`/level auto`
        при недоступном автовыборе), получает ответ с причиной, а не
        молчаливое игнорирование."""
        unavailable = (
            toggle_unavailable_hint(self.app, name, arg) if arg else None
        )
        if unavailable:
            self._show_footer_refusal(f"/{name} {arg} — {unavailable}")
            return
        self._show_footer_hint(
            toggle_info_line(name, toggle_state(self.app, name)), seconds=5.0
        )

    def _open_event_by_no(self, no: int) -> None:
        """Открыть полный payload события №no в Viewer.
        Триггер — двойной клик по строке tool-вызова / агентской реплики."""
        for ev in self._task_events:
            if ev.no == no:
                from gc_tui.screens.viewer import ViewerScreen

                # ViewerScreen сам делает render_payload — pretty-print JSON
                # с solarized-dark подсветкой для dict/list или plain Static
                # для остального. Передаём raw payload без предварительного dumps.
                # summary: в segments-пути full_payload пуст ({}); показываем
                # синтез сводки (summary_content заполнен в обоих путях).
                content = (
                    ev.summary_content
                    if ev.kind == "summary"
                    else ev.full_payload
                )
                self.app.push_screen(
                    ViewerScreen(
                        title=f"event {no} · {ev.tool_name or ev.kind}",
                        content=content,
                    )
                )
                return
        self._mount_trace_row(
            prefix="[!] ",
            content=f"нет события {no}",
            kind_class="line-error",
        )

    def on_open_event_request(self, message) -> None:
        """Двойной клик по строке tool-вызова/реплики → открыть payload."""
        self._open_event_by_no(message.event_no)

    def on_open_image_request(self, message: OpenImageRequest) -> None:
        """Клик по плашке или строке картинки в ленте, Ctrl+клик по плашке в поле ввода —
        открыть файл просмотрщиком ОС (`services/image_open`); отказ — в футер."""
        refusal = open_image(message.path)
        if refusal:
            self._show_footer_refusal(refusal)

    def on_summary_toggle_request(self, message) -> None:
        """Клик/Enter по маркеру или телу → раскрыть/свернуть summary-body."""
        body = self._summary_body_widgets.get(message.event_no)
        if body is None:
            return
        marker = self._summary_marker_widgets.get(message.event_no)
        expand = body.has_class("hidden")
        body.set_class(not expand, "hidden")
        if marker is not None:
            marker.set_expanded(expand)
        if expand:
            # Раскрыли — подвести тело в зону видимости после reflow.
            self.call_after_refresh(body.scroll_visible, animate=False)

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
        if call_ev is not None and is_light_payload(call_ev):
            self._show_footer_refusal(RETENTION_DETAIL_HINT)
            return
        if call_ev is None:
            call_ev, result_ev = self._resolve_nested_detail_events(
                message.call_id
            )
        if call_ev is None:
            if message.call_id and message.call_id in self._nested_tool_rows:
                self._show_footer_refusal(NESTED_NO_DETAIL)
            return
        self.app.push_screen(
            ToolCallDetailScreen(
                call_event=call_ev,
                result_event=result_ev,
                edit_record=self._edit_record_for_call(message.call_id),
            )
        )

    def _edit_record_for_call(self, call_id: str):
        """Запись журнала правок по call_id — карточке для рендера диффа.

        Нет журнала/записи или сбой чтения — None: карточка покажет сырой
        payload, как до волны 2."""
        if not call_id:
            return None
        try:
            lookup = self._edits_lookup_for_thread()
            return None if lookup is None else lookup.by_call_id(call_id)
        except Exception as exc:
            self.log.warning(
                f"запись правки для карточки не прочитана: {exc!r}"
            )
            return None

    def _resolve_nested_detail_events(self, call_id: str):
        """TraceEvent pair для nested tool; нет данных → (None, None)."""
        if not call_id:
            return None, None
        records = None
        if self._store is not None and self._active_task_id:
            records = self._store.read_subagent_trace(self._active_task_id)
        parent = nested_call_parent_id(records or [], call_id)
        if parent and parent_outside_retention(self._task_events, parent):
            return None, None
        detail = resolve_nested_tool_detail(call_id, sidecar_records=records)
        if not detail or not isinstance(detail.get("call"), dict):
            return None, None
        return trace_events_from_nested_detail(
            call_id, detail, root=self._current_cwd()
        )

    async def _fetch_skills(self) -> list[dict]:
        """Список скиллов из core-реестра (обход диска — вне цикла
        интерфейса); ошибки — вызывающему."""
        from gc_tui.services.skills_scan import list_skills_off_loop

        return await list_skills_off_loop(self.app.base_settings())

    @property
    def _skills_cache(self) -> Optional[list[dict]]:
        return self._skills_cache_value

    @_skills_cache.setter
    def _skills_cache(self, value: Optional[list[dict]]) -> None:
        """Кэш скиллов; смена перерисовывает поле ввода: подсветка `/имени`
        зависит от кэша (холодный — не подсвечивает)."""
        self._skills_cache_value = value
        try:
            inp = self.query_one(_SEL_CMDINPUT, CommandTextArea)
        except NoMatches:
            return
        inp.refresh_command_highlight()

    def _warm_skills_cache(self) -> None:
        """Тихий фоновый прогрев `_skills_cache` (спека P5 §3.1): палитре
        скиллы нужны с первой буквы, без I/O на нажатие. Ошибка — только в
        лог: кэш остаётся холодным, явный /skill загрузит и покажет её сам.
        `exclusive` отменяет прежний прогрев: его устаревший снимок в кэш не
        попадёт; воркер экрана отменяется и при его снятии."""
        self.run_worker(
            self._warm_skills(), group="skills-warm", exclusive=True
        )

    async def _warm_skills(self) -> None:
        try:
            skills = await self._fetch_skills()
        except Exception:
            _log.warning("прогрев кэша скиллов не удался", exc_info=True)
            return
        if self._skills_cache is None:
            self._skills_cache = skills

    async def _load_skills_safe(self) -> list[dict]:
        """Загрузить список скиллов из core-реестра; на ошибке вернуть []."""
        try:
            return await self._fetch_skills()
        except Exception as exc:
            self._mount_trace_row(
                prefix="[!] ",
                content=f"/skills: не удалось загрузить ({exc})",
                kind_class="line-error",
            )
            return []

    def _cmd_export(self, arg: str) -> None:
        fmt = (arg or "").strip() or "txt"
        if fmt not in ("txt", "json", "log-txt", "log-json"):
            self._mount_trace_row(
                prefix="[!] ",
                content=f"/export: формат {fmt!r} не поддерживается",
                kind_class="line-error",
            )
            return
        tid = self._active_task_id
        if not tid:
            self._mount_trace_row(
                prefix="[!] ",
                content="/export: нет активной задачи",
                kind_class="line-error",
            )
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        if fmt == "log-txt":
            self._export_log_txt(tid, stamp)
            return
        if self._store is None:
            self._mount_trace_row(
                prefix="[!] ",
                content="/export: нет активной задачи",
                kind_class="line-error",
            )
            return
        if fmt == "log-json":
            raw = self._store.read_raw_thread_jsonl(tid)
            path = Path.cwd() / f"export-{tid}-{stamp}-log.jsonl"
            path.write_text(raw, encoding="utf-8")
            self._mount_trace_row(
                prefix=_PREFIX_EXPORTED,
                content=str(path),
                kind_class="line-meta",
            )
            return
        segments = self._store.read_segments(tid)
        meta = self._store.get_task_meta(tid)
        text = export_thread(
            segments,
            fmt=fmt,
            meta=meta,
            thread_id=tid,
            system=self.app._system,
        )
        path = Path.cwd() / f"export-{tid}-{stamp}.{fmt}"
        path.write_text(text, encoding="utf-8")
        self._mount_trace_row(
            prefix=_PREFIX_EXPORTED,
            content=str(path),
            kind_class="line-meta",
        )

    def _cmd_copy(self, arg: str) -> None:
        """/copy [N]: N-й с конца ответ агента — в буфер обмена; итог — в
        слоте футера."""
        self._copy_seq += 1
        answers = []
        if self._store is not None and self._active_task_id:
            answers = agent_answers(
                self._store.read_segments(self._active_task_id)
            )
        text = (arg or "").strip() or "1"
        if not text.isdecimal() or int(text) < 1:
            self._show_footer_outcome(COPY_REFUSAL_BAD_N, ok=False)
            return
        n, total = int(text), len(answers)
        if not total:
            self._show_footer_outcome(COPY_REFUSAL_EMPTY, ok=False)
        elif n > total:
            self._show_footer_outcome(
                COPY_REFUSAL_TOO_BIG.format(n=n, total=total), ok=False
            )
        else:
            done = (
                COPY_DONE_LAST
                if n == 1
                else COPY_DONE_NTH.format(n=n, total=total)
            )
            # Запись идёт в фоне: слот молчит, пока не пришёл её итог.
            # Итог прошлого /copy, пришедший позже нового, слот не трогает.
            seq = self._copy_seq

            def show(ok: bool) -> None:
                if seq == self._copy_seq:
                    self._show_footer_outcome(
                        done if ok else COPY_REFUSAL_FAILED, ok=ok
                    )

            copy_out(
                self.app, answers[-n], blocking=False, quiet=True, on_done=show
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
                prefix="[!] ",
                content="/export: логи отключены (save_logs: false)",
                kind_class="line-error",
            )
            return
        src = Path(log_path)
        if not src.exists():
            self._mount_trace_row(
                prefix="[!] ",
                content=f"/export: файл лога не найден: {src}",
                kind_class="line-error",
            )
            return
        dst = Path.cwd() / f"export-{tid}-{stamp}-{src.name}"
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        self._mount_trace_row(
            prefix=_PREFIX_EXPORTED,
            content=str(dst),
            kind_class="line-meta",
        )

    def _cmd_status(self, arg: str) -> None:
        """Дамп meta активной задачи + runtime-состояние TUI в Viewer.

        Флаг `--raw` принимается в `arg`, но MVP-формат один (JSON с отступами);
        семантика raw vs formatted зарезервирована за будущим.
        """
        from gc_tui.screens.viewer import ViewerScreen

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
        active_profile = self.app.effective_settings().runtime.active_profile
        router = self.app.display_router()
        # check_breaker=False: /status показывает модель честно даже при открытом
        # активном алиасе (спека §12.2); недоступность отражаем отдельным флагом.
        resolved = router.resolve("main", check_breaker=False)
        breaker_open = bool(resolved.alias) and router.breaker.is_open(
            resolved.alias
        )
        ref = self.app.active_profile_ref()
        # Действующий уровень — то же правило, что у палитры и футера: `auto`,
        # только когда автовыбор реально активен, иначе уровень, которым идёт работа.
        level = toggle_state(self.app, "level")
        payload["runtime"] = {
            "active_task_id": tid,
            "runner_is_running": bool(runner and runner.is_running()),
            "pending_skill_id": self.app.tui_state.pending_skill_id,
            "ask_human": (
                {"call_id": ah.call_id, "question": ah.question}
                if ah is not None
                else None
            ),
            "last_runner_error": getattr(self.app, "last_runner_error", None),
            "profile": {
                "name": active_profile or None,
                "source": _profile_source_label(ref),
                "level": level,
                "routing_policy": ("two_level" if level == "auto" else None),
                "model": resolved.alias or resolved.model,
                "circuit_breaker_open": breaker_open,
            },
        }

        self.app.push_screen(
            ViewerScreen(
                title=f"status · {title_id}",
                content=json.dumps(payload, ensure_ascii=False, indent=2),
            )
        )

    def _cmd_compact(self) -> None:
        """/compact — принудительно сжать сессию (гарды: режим/простой/контент)."""
        settings = self.app.base_settings().settings
        if str(settings.backend.api_mode) != "chat_completions":
            self._mount_trace_row(
                prefix="· ",
                content="/compact недоступен в режиме responses",
                kind_class="line-meta",
            )
            return
        tid = self._active_task_id
        runner = getattr(self.app, "active_runner", None)
        if tid is None or self._store is None:
            self._mount_trace_row(
                prefix="· ",
                content="/compact: нет активной сессии",
                kind_class="line-meta",
            )
            return
        if (
            runner is not None and runner.is_running()
        ) or self._task_status_badge() in ("[running]", "[asking]"):
            self._mount_trace_row(
                prefix="· ",
                content="/compact: дождитесь паузы или /stop",
                kind_class="line-meta",
            )
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
                kind_class="line-meta",
            )
            return
        if runner is None:
            runner = self.app.get_runner()
        # Сброс якоря таймера: «сжимаю контекст…» должен считать с 0, а не от
        # момента прошлого хода (как и на обычном send).
        self._reset_thinking_timer()
        if runner.compact_now(tid, self.app._system):
            # Сжатие — работа: бар «сжимаю контекст…» (S10, решение В3).
            self._run_pending = True
            self._sync_indicator_bar()

    def _cmd_init(self) -> None:
        """/init: видимый prompt-driven прогон — агент сам пишет AGENTS.md.

        Зеркалит _deliver_skill_with_prompt: init-промпт идёт как ОБЫЧНОЕ user-сообщение
        (wire), в трейсе — маркер "/init" (display). busy-guard отбивает активный прогон,
        поэтому здесь только два случая: auto-create треда и continue-thread."""
        runner = self.app.get_runner()
        if runner.is_running():
            self._mount_trace_row(
                prefix="[!] ",
                content="/init: дождитесь завершения текущей задачи",
                kind_class="line-error",
            )
            return

        from gc_core.instructions.init_agents_md import build_init_prompt

        wire = build_init_prompt(cwd=os.getcwd(), can_ask=True)
        display = "/init"
        msg_id = "m_" + uuid.uuid4().hex
        self._last_user_message_id = msg_id

        if self.app.tui_state.active_task_id is None:
            tid = self._begin_auto_created_task()
            runner = self.app.get_runner()
            client_id = self._track_pending(
                display_text=display, wire_content=wire
            )
            self._mount_user_reply(
                display, state="pending", client_id=client_id
            )
            self._mount_trace_row(
                prefix="",
                content="/init: запускаю агента",
                kind_class="line-meta",
            )
            self._prepare_for_new_run()
            self._reset_thinking_timer()
            self._on_user_text_submitted(display)
            runner.start(
                query=wire,
                thread_id=tid,
                system=self.app._system,
                message_id=msg_id,
            )
            return

        runner = self.app.get_runner()
        client_id = self._track_pending(
            display_text=display, wire_content=wire
        )
        self._mount_user_reply(display, state="pending", client_id=client_id)
        self._mount_trace_row(
            prefix="", content="/init: запускаю агента", kind_class="line-meta"
        )
        self._prepare_for_new_run()
        self._reset_thinking_timer()
        self._on_user_text_submitted(display)
        runner.start(
            query=wire,
            thread_id=self.app.tui_state.active_task_id,
            system=self.app._system,
            message_id=msg_id,
        )

    def _render_command_error(
        self,
        name: str,
        suggestions: list[str],
        *,
        title: str = "неизвестная команда",
    ) -> None:
        """Команда-ошибка по единому правилу (§3.3). Одна строка одним Static;
        кандидаты «вы имели в виду» — deep-dim в той же строке."""
        from rich.text import Text

        line = Text()
        line.append(f"[!] {title}: /{name}", style=_ERR_RED)
        if suggestions:
            names = ", ".join(f"/{s}" for s in suggestions)
            line.append(f" (вы имели в виду {names}?)", style=_DEEP_DIM)
        self._trace_mount(Static(line, classes="line-error", markup=False))

    def _render_skill_ambiguous(self, name: str, ids: list[str]) -> None:
        """Короткое имя у нескольких скиллов — не запуск, подсказать id."""
        self._render_command_error(
            name, ids, title="несколько скиллов с именем"
        )

    def _mount_trace_row(
        self,
        *,
        prefix: str,
        content: str,
        kind_class: str,
        event_no: int = 0,
        focusable: bool = True,
    ) -> TraceRow:
        """Смонтировать строку трассы через TraceRow (Horizontal prefix + content).

        focusable=True (дефолт) — для самостоятельных одно-строчных событий.
        focusable=False — для continuation/декорация-строк, уже фокусируемых
        главным виджетом события (например, `* note:` под submit_solution)."""
        from gc_tui.widgets.trace_row import TraceRow

        row = TraceRow(
            prefix=prefix,
            content=content,
            kind_class=kind_class,
            event_no=event_no,
            focusable=focusable,
        )
        self._trace_mount(row)
        return row
