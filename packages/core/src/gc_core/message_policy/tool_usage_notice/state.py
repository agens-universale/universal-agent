from __future__ import annotations

from typing import Any

META_PROJECT_MEMORY_CONTENT_HASH = "project_memory_content_hash"
META_TOOL_USAGE_NOTICE_CNT = "tool_usage_notice_cnt"
META_MEMORY_NOTICE_CNT = "memory_notice_cnt"
META_PROJECT_MEMORY_CHECK_CNT = "project_memory_check_cnt"
META_TODO_REMINDER_CNT = "todo_reminder_cnt"

STATE_META_KEYS = (
    META_PROJECT_MEMORY_CONTENT_HASH,
    META_TOOL_USAGE_NOTICE_CNT,
    META_TODO_REMINDER_CNT,
)
EPHEMERAL_META_KEYS = (META_PROJECT_MEMORY_CHECK_CNT,)
PERSISTED_META_KEYS = STATE_META_KEYS + EPHEMERAL_META_KEYS


def _coerce_non_negative_int(value: Any, *, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _coerce_tool_usage_notice_counters(value: Any) -> dict[str, int]:
    if isinstance(value, dict):
        raw = value
    else:
        return {}
    out: dict[str, int] = {}
    for key, item in raw.items():
        if not isinstance(key, str) or not key.strip():
            continue
        out[key.strip()] = _coerce_non_negative_int(item)
    return out


def hydrate_tool_usage_notice_state(
    state: Any, meta: dict[str, Any] | None
) -> None:
    """Восстановить поля State и meta-only ключи из thread/child meta."""
    session = meta if isinstance(meta, dict) else {}
    raw_hash = session.get(META_PROJECT_MEMORY_CONTENT_HASH)
    if raw_hash is None or isinstance(raw_hash, str):
        state.project_memory_content_hash = raw_hash
    else:
        state.project_memory_content_hash = None

    counters = _coerce_tool_usage_notice_counters(
        session.get(META_TOOL_USAGE_NOTICE_CNT)
    )
    if not counters and META_MEMORY_NOTICE_CNT in session:
        legacy = _coerce_non_negative_int(
            session.get(META_MEMORY_NOTICE_CNT, 0)
        )
        if legacy > 0:
            counters = {"session_memory": legacy}
    state.tool_usage_notice_cnt = counters
    # Счётчик протухания чек-листа: без переноса через meta задача из серии
    # коротких ходов никогда не докатывается до напоминания (State — per-run).
    state.todo_reminder_cnt = _coerce_non_negative_int(
        session.get(META_TODO_REMINDER_CNT, 0)
    )


def persist_tool_usage_notice_state(state: Any, meta: dict[str, Any]) -> None:
    """Записать поля State в session meta (in-memory thread_session.meta / child_meta)."""
    meta[META_PROJECT_MEMORY_CONTENT_HASH] = getattr(
        state, "project_memory_content_hash", None
    )
    raw_counters = getattr(state, "tool_usage_notice_cnt", None)
    meta[META_TOOL_USAGE_NOTICE_CNT] = (
        _coerce_tool_usage_notice_counters(raw_counters)
        if isinstance(raw_counters, dict)
        else {}
    )
    meta[META_TODO_REMINDER_CNT] = _coerce_non_negative_int(
        getattr(state, "todo_reminder_cnt", 0)
    )


def merge_tool_usage_notice_meta_to_base(
    base_meta: dict[str, Any],
    session_meta: dict[str, Any],
) -> None:
    """Слить ключи tool_usage_notice / project_memory из session meta в base_meta перед save."""
    if not isinstance(session_meta, dict):
        return
    for key in PERSISTED_META_KEYS:
        if key in session_meta:
            base_meta[key] = session_meta[key]


def get_project_memory_check_cnt(meta: dict[str, Any]) -> int:
    if not isinstance(meta, dict):
        return 0
    return _coerce_non_negative_int(meta.get(META_PROJECT_MEMORY_CHECK_CNT, 0))


def set_project_memory_check_cnt(meta: dict[str, Any], value: int) -> None:
    meta[META_PROJECT_MEMORY_CHECK_CNT] = _coerce_non_negative_int(value)
