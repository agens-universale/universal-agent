"""UI-модели TUI MVP — §3 спеки. Без textual."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Union

from gc_core.attachments.model import Attachment

Kind = Literal[
    "user",
    "agent",
    "call",
    "result",
    "error",
    "error_msg",
    "system",
    "ask_human",
    "summary",
    "todo",
]
Status = Literal["running", "stopped", "completed", "failed"]


@dataclass
class TaskCursor:
    """Opaque-курсор tail-чтения. Backend-agnostic (tui3_impl §6.1)."""

    position_token: Any
    ordering_key: Optional[Union[int, str]]
    storage_epoch: Optional[str]
    archives: int = 0
    wait_ticks: int = 0


@dataclass
class CompactionStep:
    """Шаг сжатия как данные: что дорисовать в трейс вместо перестройки."""

    archive_tail: list[dict]
    segment_index: int
    compacted_by: dict
    # id окна retention после шага: архив и новый живой файл.
    window_ids: frozenset[str]
    live_records: list[dict]


@dataclass
class TailResult:
    """Результат tail_events (tui3_impl §6.3)."""

    new_records: list[dict]
    next_cursor: TaskCursor
    stream_reset: bool = False
    reason: Optional[str] = None
    compaction: Optional[CompactionStep] = None


@dataclass
class TaskSummary:
    """Строка списка задач (tui3_impl §10.1)."""

    thread_id: str
    status: Optional[
        Status
    ]  # None — исход не записан (легаси / прогон без чистого финиша)
    updated_at: str
    messages_count: int
    skill_id: Optional[str]
    unread_count: int
    last_error: Optional[str]
    tui_meta: Optional[dict] = None
    token_usage: Optional[dict] = (
        None  # meta.token_usage (UsageAccount.to_meta)
    )


@dataclass
class TraceEvent:
    """Событие ленты трейса (tui3 §521, tui3_impl §4.2)."""

    no: int
    event_ts_utc_iso: str
    kind: Kind
    title: str
    summary: str
    full_payload: Union[dict, str]
    message_id: Optional[str] = None
    call_id: Optional[str] = None
    tool_name: Optional[str] = None
    # wire-имя тула — как его зовёт реестр (fs_append_file, а не append_to_file);
    # заполняется только у kind="call" (Д11/R3), только для ПОКАЗА в ToolCallRow.
    # tool_name остаётся каноническим id — на нём завязана логика маппера/экрана
    # (COGNITIVE_TOOLS, "intermediate_reply"/"submit_solution"/"subagent_*" и т.д.).
    wire_name: Optional[str] = None
    # Сводка аргументов вызова от ядра (`tool_args_summary`) — объект строки
    # вызова; заполняется только у kind="call".
    args_summary: str = ""
    hidden: bool = False  # True → не рендерить в трейс (но kind учитывается polling-логикой)
    summary_content: str = (
        ""  # очищенный текст сводки (только для kind="summary")
    )
    # Картинки реплики (kind="user"): вложения сообщения, а у ответа на
    # turn_ask_human — из следующего технического сообщения (дизайн картинок §20.3).
    attachments: tuple[Attachment, ...] = ()
    # Результат вызова — стоп пользователя или лимит, а не сбой тула: строка
    # вызова `■` и серое `└` (K4), не `✗` (только у kind="error").
    halted: bool = False
