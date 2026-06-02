from __future__ import annotations

from typing import Any

META_LONG_MEMORY_CONTENT_HASH = "long_memory_content_hash"
META_TOOL_USAGE_NOTICE_CNT = "tool_usage_notice_cnt"
META_MEMORY_NOTICE_CNT = "memory_notice_cnt"
META_LONG_MEMORY_CHECK_CNT = "long_memory_check_cnt"

STATE_META_KEYS = (
    META_LONG_MEMORY_CONTENT_HASH,
    META_TOOL_USAGE_NOTICE_CNT,
)
EPHEMERAL_META_KEYS = (META_LONG_MEMORY_CHECK_CNT,)
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


def hydrate_tool_usage_notice_state(state: Any, meta: dict[str, Any] | None) -> None:
    """Восстановить поля State и meta-only ключи из thread/child meta."""
    session = meta if isinstance(meta, dict) else {}
    raw_hash = session.get(META_LONG_MEMORY_CONTENT_HASH)
    if raw_hash is None or isinstance(raw_hash, str):
        state.long_memory_content_hash = raw_hash
    else:
        state.long_memory_content_hash = None

    counters = _coerce_tool_usage_notice_counters(session.get(META_TOOL_USAGE_NOTICE_CNT))
    if not counters and META_MEMORY_NOTICE_CNT in session:
        legacy = _coerce_non_negative_int(session.get(META_MEMORY_NOTICE_CNT, 0))
        if legacy > 0:
            counters = {"working_memory": legacy}
    state.tool_usage_notice_cnt = counters


def persist_tool_usage_notice_state(state: Any, meta: dict[str, Any]) -> None:
    """Записать поля State в session meta (in-memory thread_session.meta / child_meta)."""
    meta[META_LONG_MEMORY_CONTENT_HASH] = getattr(
        state, "long_memory_content_hash", None
    )
    raw_counters = getattr(state, "tool_usage_notice_cnt", None)
    meta[META_TOOL_USAGE_NOTICE_CNT] = (
        _coerce_tool_usage_notice_counters(raw_counters)
        if isinstance(raw_counters, dict)
        else {}
    )


def merge_tool_usage_notice_meta_to_base(
    base_meta: dict[str, Any],
    session_meta: dict[str, Any],
) -> None:
    """Слить ключи tool_usage_notice / long_memory из session meta в base_meta перед save."""
    if not isinstance(session_meta, dict):
        return
    for key in PERSISTED_META_KEYS:
        if key in session_meta:
            base_meta[key] = session_meta[key]


def get_long_memory_check_cnt(meta: dict[str, Any]) -> int:
    if not isinstance(meta, dict):
        return 0
    return _coerce_non_negative_int(meta.get(META_LONG_MEMORY_CHECK_CNT, 0))


def set_long_memory_check_cnt(meta: dict[str, Any], value: int) -> None:
    meta[META_LONG_MEMORY_CHECK_CNT] = _coerce_non_negative_int(value)
