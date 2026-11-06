"""Subagent progress transport: events + sink (runtime → TUI drain).

Domain/runtime owns emit; TUI only consumes. Default sink is no-op so CLI/server
stay unchanged. Events are never persisted to thread jsonl.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    List,
    Literal,
    Optional,
    Protocol,
    runtime_checkable,
)

from gc_core.constants.tool_display import (
    LEGACY_TOOL_NAMES,
    DisplayCategory,
    tool_display,
)
from gc_core.constants.tool_names import (
    EXEC_PYTHON_NAME,
    FS_FILE_INFO_NAME,
    FS_FIND_IN_FILES_NAME,
    FS_LIST_FILES_NAME,
    SESSION_TODO_TOOL_NAME,
    SKILLS_READ_ASSET_NAME,
    SKILLS_READ_REFERENCE_NAME,
)
from gc_core.utils.path_truncate import truncate_path

ProgressKind = Literal[
    "started",
    "tool_start",
    "tool_end",
    "intermediate_reply",
    "user_message",
    "ask_human",
    "ask_human_done",
    "confirm",
    "confirm_done",
    "usage",
    "heartbeat",
    "finished",
    "error",
    "cancelled",
]

_ERROR_BRIEF_MAX = 120
_ARGS_SUMMARY_MAX = 80


@dataclass(frozen=True)
class SubagentProgressEvent:
    parent_call_id: str
    child_run_id: str
    kind: ProgressKind
    step: int
    max_steps: int
    elapsed_ms: int
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    args_summary: Optional[str] = None
    prose: Optional[str] = None
    ok: Optional[bool] = None
    error_brief: Optional[str] = None  # ≤120 chars when set by emitters
    usage_delta: Optional[Dict[str, Any]] = None
    todo_items: Optional[List[Dict[str, str]]] = None
    # Картинки реплики человека ребёнку (``user_message``) — словари вложений в
    # треде, как в записях треда.
    attachments: Optional[List[Dict[str, Any]]] = None


@runtime_checkable
class SubagentProgressSink(Protocol):
    def emit(self, event: SubagentProgressEvent) -> None: ...


class NoOpSubagentProgressSink:
    """Default sink outside TUI — emit is a no-op."""

    def emit(self, event: SubagentProgressEvent) -> None:
        """Intentionally does nothing: progress is dropped outside the TUI."""


NOOP_SUBAGENT_PROGRESS_SINK = NoOpSubagentProgressSink()


class QueueSubagentProgressSink:
    """Thread-safe queue: runtime emits from worker thread; TUI drains on poll."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: Deque[SubagentProgressEvent] = deque()

    def emit(self, event: SubagentProgressEvent) -> None:
        with self._lock:
            self._queue.append(event)

    def drain(self) -> List[SubagentProgressEvent]:
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
            return items


def resolve_progress_sink(ctx: Any) -> SubagentProgressSink:
    """Return ctx.subagent_progress_sink or the shared no-op."""
    sink = getattr(ctx, "subagent_progress_sink", None)
    if sink is None:
        return NOOP_SUBAGENT_PROGRESS_SINK
    return sink


def child_progress_enabled(ctx: Any) -> bool:
    """True only for nested child runs that have a sink (parent_call_id set)."""
    if not getattr(ctx, "parent_call_id", None):
        return False
    return getattr(ctx, "subagent_progress_sink", None) is not None


def _todo_args_summary(todos: Any, *, limit: int = _ARGS_SUMMARY_MAX) -> str:
    """`чек-лист K/M · текущий пункт` — одной строкой для строки трассы.

    Запасной путь для потребителей, которые не умеют рисовать развёрнутый блок;
    ``limit`` соблюдается как во всех прочих ветках ``tool_args_summary`` —
    иначе длинный пункт расползается на несколько строк.
    """
    total = 0
    done = 0
    current = ""
    for item in todos if isinstance(todos, list) else []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        status = item.get("status")
        if not content:
            continue
        total += 1
        if status == "completed":
            done += 1
        elif status == "in_progress" and not current:
            current = content
    if not total:
        return "чек-лист очищен"
    head = f"чек-лист {done}/{total}"
    text = f"{head} · {current}" if current else head
    return _clip(text, limit)


_TODO_ITEM_STATUSES = ("pending", "in_progress", "completed")


def todo_items_from_arguments(
    arguments: Any,
) -> Optional[List[Dict[str, str]]]:
    """Пункты чек-листа из аргументов вызова session_todo — или ``None``.

    Понимает str-JSON и dict. Валидация ЗЕРКАЛИТ тул (``tools/session/todo.py``):
    он отклоняет вызов целиком на первом же кривом пункте и состояние не меняет,
    поэтому и рендер обязан классифицировать «всё или ничего». Иначе частично
    битый вызов рисуется как «чек-лист очищен» при живом списке.

    ``None`` — аргументы не читаются как чек-лист (битый JSON, нет ``todos``,
    ``todos`` не список, любой невалидный пункт): такой вызов рендерится обычной
    tool-строкой, где статус сбоя виден механикой. ``[]`` — валидная очистка."""
    data: Any = arguments
    if isinstance(arguments, str):
        try:
            data = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return None
    todos = data.get("todos") if isinstance(data, dict) else None
    if not isinstance(todos, list):
        return None
    out: List[Dict[str, str]] = []
    for item in todos:
        normalized = _todo_item_from(item)
        if normalized is None:
            return None
        out.append(normalized)
    return out


def _todo_item_from(item: Any) -> Optional[Dict[str, str]]:
    """Валидный пункт {content, status} или None (зеркалит проверку тула)."""
    if not isinstance(item, dict) or set(item) - {"content", "status"}:
        return None
    content = item.get("content")
    status = item.get("status")
    if not isinstance(content, str) or not content.strip():
        return None
    if status not in _TODO_ITEM_STATUSES:
        return None
    return {"content": content.strip(), "status": status}


def tool_args_summary(
    wire_name: Optional[str],
    arguments: Any,
    *,
    limit: int = _ARGS_SUMMARY_MAX,
) -> str:
    """Однострочная сводка аргументов вызова тула (спека P1a §2.5).

    Главный аргумент — по таблице отображения ядра; его нет — до трёх пар
    ``key=preview``. Длина сводки не превышает ``limit``."""
    data: Any = arguments
    if isinstance(arguments, str):
        try:
            data = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return _clip(arguments, limit)
    name = LEGACY_TOOL_NAMES.get(wire_name, wire_name) if wire_name else None
    if name == SESSION_TODO_TOOL_NAME:
        items = todo_items_from_arguments(data)
        if items is not None:
            return _todo_args_summary(items, limit=limit)
        # битые аргументы — генерик-превью, «чек-лист очищен» не врём
    if not isinstance(data, dict) or not data:
        return ""
    primary = _primary_summary(name, data, limit) if name else None
    if primary:
        return _clip(primary, limit)
    return _pairs_summary(data, limit)


def _clip(text: str, limit: int) -> str:
    """Схлопнуть пробелы и обрезать до ``limit`` символов вместе с ``…``."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 0)].rstrip() + "…"


def _primary_summary(name: str, data: Dict[str, Any], limit: int) -> str:
    """Сводка по главному аргументу; пустая строка — главного нет."""
    shown = tool_display(name)
    if shown.category is DisplayCategory.MCP:
        return _first_scalar(data)
    key = shown.primary_arg
    value = data.get(key) if key else None
    if not isinstance(value, str) or not value.strip():
        return ""
    rule = _PRIMARY_RULES.get(name)
    if rule is not None:
        return rule(value, data, limit)
    if key == "path":
        return truncate_path(value, limit)
    return value


def _first_scalar(data: Dict[str, Any]) -> str:
    for value in data.values():
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            return str(value)
    return ""


def _find_in_files(pattern: str, data: Dict[str, Any], limit: int) -> str:
    head = f'"{pattern}"'
    path = data.get("path")
    if not isinstance(path, str) or not path:
        return head
    head += " in "
    return head + truncate_path(path, limit - len(head))


def _list_files(path: str, _data: Dict[str, Any], limit: int) -> str:
    if path.endswith(("/", "\\")):
        return truncate_path(path, limit - 1) + "*"
    return truncate_path(path, limit - 2) + "/*"


def _file_info(path: str, _data: Dict[str, Any], limit: int) -> str:
    suffix = " · info"
    return truncate_path(path, limit - len(suffix)) + suffix


def _python_code(code: str, _data: Dict[str, Any], _limit: int) -> str:
    count = len(code.splitlines())
    return f"python · {count} {'line' if count == 1 else 'lines'}"


def _skill_file(skill_id: str, data: Dict[str, Any], _limit: int) -> str:
    slug = data.get("slug")
    if isinstance(slug, str) and slug.strip():
        return f"{skill_id} · {slug}"
    return skill_id


_PRIMARY_RULES: Dict[str, Callable[[str, Dict[str, Any], int], str]] = {
    FS_FIND_IN_FILES_NAME: _find_in_files,
    FS_LIST_FILES_NAME: _list_files,
    FS_FILE_INFO_NAME: _file_info,
    EXEC_PYTHON_NAME: _python_code,
    SKILLS_READ_REFERENCE_NAME: _skill_file,
    SKILLS_READ_ASSET_NAME: _skill_file,
}


def _pairs_summary(data: Dict[str, Any], limit: int) -> str:
    pairs = [
        _arg_pair_preview(key, value) for key, value in list(data.items())[:3]
    ]
    return _clip(", ".join(pairs), limit)


def _arg_pair_preview(key: str, value: Any) -> str:
    if isinstance(value, str):
        preview = " ".join(value.split())
        if len(preview) > 40:
            preview = preview[:40].rstrip() + "…"
        return f"{key}={preview!r}"
    if isinstance(value, (int, float, bool)) or value is None:
        return f"{key}={value!r}"
    if isinstance(value, list):
        return f"{key}=<list {len(value)}>"
    return f"{key}=<?>"


def _truncate_error_brief(message: str) -> str:
    text = " ".join(str(message).split())
    if len(text) <= _ERROR_BRIEF_MAX:
        return text
    return text[:_ERROR_BRIEF_MAX].rstrip() + "…"


def parse_tool_result_status(
    output: Any,
) -> tuple[Optional[bool], Optional[str]]:
    """Extract (ok, error_brief) from a tool result payload string/dict."""
    data: Any = output
    if isinstance(output, str):
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return None, None
    if not isinstance(data, dict):
        return None, None
    ok = data.get("ok")
    if not isinstance(ok, bool):
        ok = None
    brief = _error_brief_from_payload(data) if ok is False else None
    return ok, brief


def _error_brief_from_payload(data: Dict[str, Any]) -> Optional[str]:
    err = data.get("error")
    if isinstance(err, dict):
        # reason раньше message — как в ленте TUI (trace_mapper): причина
        # человеку, а не инструкция модели.
        msg = err.get("reason") or err.get("message") or err.get("code") or ""
        return _truncate_error_brief(str(msg)) if msg else None
    if isinstance(err, str) and err.strip():
        return _truncate_error_brief(err)
    if isinstance(data.get("message"), str) and data["message"].strip():
        return _truncate_error_brief(data["message"])
    return None


def extract_intermediate_prose(arguments: Any, output: Any) -> Optional[str]:
    """Prefer answer from tool output; fall back to call arguments."""
    for candidate in (output, arguments):
        data: Any = candidate
        if isinstance(candidate, str):
            try:
                data = json.loads(candidate) if candidate.strip() else {}
            except json.JSONDecodeError:
                continue
        if isinstance(data, dict):
            answer = data.get("answer")
            if isinstance(answer, str) and answer.strip():
                return answer.strip()
    return None


def emit_progress(
    ctx: Any,
    state: Any,
    kind: ProgressKind,
    *,
    elapsed_ms: int = 0,
    tool_name: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    args_summary: Optional[str] = None,
    prose: Optional[str] = None,
    ok: Optional[bool] = None,
    error_brief: Optional[str] = None,
    usage_delta: Optional[Dict[str, Any]] = None,
    todo_items: Optional[List[Dict[str, str]]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Emit a progress event when running as a child with a sink; else no-op."""
    if not child_progress_enabled(ctx):
        return
    sink = ctx.subagent_progress_sink
    if error_brief is not None:
        error_brief = _truncate_error_brief(error_brief)
    sink.emit(
        SubagentProgressEvent(
            parent_call_id=str(ctx.parent_call_id),
            child_run_id=str(getattr(ctx, "run_id", "") or ""),
            kind=kind,
            step=int(getattr(state, "steps", 0) or 0),
            max_steps=int(getattr(state, "max_steps", 0) or 0),
            elapsed_ms=int(elapsed_ms),
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            args_summary=args_summary,
            prose=prose,
            ok=ok,
            error_brief=error_brief,
            usage_delta=usage_delta,
            todo_items=todo_items,
            attachments=attachments,
        )
    )


__all__ = [
    "NOOP_SUBAGENT_PROGRESS_SINK",
    "NoOpSubagentProgressSink",
    "ProgressKind",
    "QueueSubagentProgressSink",
    "SubagentProgressEvent",
    "SubagentProgressSink",
    "child_progress_enabled",
    "emit_progress",
    "extract_intermediate_prose",
    "parse_tool_result_status",
    "resolve_progress_sink",
    "tool_args_summary",
]
