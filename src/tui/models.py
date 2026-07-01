"""UI-модели TUI MVP — §3 спеки. Без textual."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Union


Kind = Literal["user", "agent", "call", "result", "error", "error_msg", "system", "ask_human", "summary"]
Status = Literal["running", "stopped", "completed", "failed"]


@dataclass
class TaskCursor:
    """Opaque-курсор tail-чтения. Backend-agnostic (tui3_impl §6.1)."""

    position_token: Any
    ordering_key: Optional[Union[int, str]]
    storage_epoch: Optional[str]


@dataclass
class TailResult:
    """Результат tail_events (tui3_impl §6.3)."""

    new_records: list[dict]
    next_cursor: TaskCursor
    stream_reset: bool = False
    reason: Optional[str] = None


@dataclass
class TaskSummary:
    """Строка списка задач (tui3_impl §10.1)."""

    thread_id: str
    status: Optional[Status]   # None — исход не записан (легаси / прогон без чистого финиша)
    updated_at: str
    messages_count: int
    skill_id: Optional[str]
    unread_count: int
    last_error: Optional[str]
    tui_meta: Optional[dict] = None


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
    hidden: bool = False  # True → не рендерить в трейс (но kind учитывается polling-логикой)
    summary_content: str = ""  # очищенный текст сводки (только для kind="summary")
