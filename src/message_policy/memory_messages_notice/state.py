from __future__ import annotations

from typing import Any

META_LONG_MEMORY_CONTENT_HASH = "long_memory_content_hash"
META_MEMORY_NOTICE_CNT = "memory_notice_cnt"
META_LONG_MEMORY_CHECK_CNT = "long_memory_check_cnt"
META_SUBAGENT_THREAD_MEMORY_ANNOUNCED = "subagent_thread_memory_announced"

STATE_META_KEYS = (
    META_LONG_MEMORY_CONTENT_HASH,
    META_MEMORY_NOTICE_CNT,
)
EPHEMERAL_META_KEYS = (
    META_LONG_MEMORY_CHECK_CNT,
    META_SUBAGENT_THREAD_MEMORY_ANNOUNCED,
)
PERSISTED_META_KEYS = STATE_META_KEYS + EPHEMERAL_META_KEYS


def _coerce_non_negative_int(value: Any, *, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def hydrate_memory_messages_state(state: Any, meta: dict[str, Any] | None) -> None:
    """Восстановить поля State и meta-only ключи из thread/child meta."""
    session = meta if isinstance(meta, dict) else {}
    raw_hash = session.get(META_LONG_MEMORY_CONTENT_HASH)
    if raw_hash is None or isinstance(raw_hash, str):
        state.long_memory_content_hash = raw_hash
    else:
        state.long_memory_content_hash = None
    state.memory_notice_cnt = _coerce_non_negative_int(
        session.get(META_MEMORY_NOTICE_CNT, 0)
    )


def persist_memory_messages_state(state: Any, meta: dict[str, Any]) -> None:
    """Записать поля State в session meta (in-memory thread_session.meta / child_meta)."""
    meta[META_LONG_MEMORY_CONTENT_HASH] = getattr(
        state, "long_memory_content_hash", None
    )
    meta[META_MEMORY_NOTICE_CNT] = _coerce_non_negative_int(
        getattr(state, "memory_notice_cnt", 0)
    )


def merge_memory_messages_meta_to_base(
    base_meta: dict[str, Any],
    session_meta: dict[str, Any],
) -> None:
    """Слить ключи memory_messages из session meta в base_meta перед save_thread_meta."""
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


def get_subagent_thread_memory_announced(meta: dict[str, Any]) -> bool:
    if not isinstance(meta, dict):
        return False
    return bool(meta.get(META_SUBAGENT_THREAD_MEMORY_ANNOUNCED))


def set_subagent_thread_memory_announced(meta: dict[str, Any], value: bool) -> None:
    meta[META_SUBAGENT_THREAD_MEMORY_ANNOUNCED] = bool(value)
