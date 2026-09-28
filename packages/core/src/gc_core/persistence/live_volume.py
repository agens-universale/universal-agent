"""Агрегаты объёма live-окна в meta.runtime (persist-owned).

Ключи ``live_history_chars`` / ``live_msg_count`` пишет thread_store при
append / compact / discard; TUI только читает (см. docs_md/tui/meta-ownership).
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from gc_core.message_format import calc_history_chars_for_trigger
from gc_core.persistence.meta_ownership import merge_runtime_into_meta


def merge_tui_into_meta(meta: Dict[str, Any], patch: Dict[str, Any]) -> None:
    """Deprecated alias: live_* теперь в ``meta.runtime``."""
    merge_runtime_into_meta(meta, patch)


def live_volume_from_messages(
    messages: List[Dict[str, Any]],
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> Dict[str, int]:
    """Полный пересчёт агрегатов объёма live-окна."""
    return {
        "live_history_chars": calc_history_chars_for_trigger(
            messages, tags=tags, strip_think=strip_think
        ),
        "live_msg_count": len(messages),
    }


def bump_live_volume_in_meta(
    meta: Dict[str, Any], *, delta_chars: int, delta_count: int
) -> None:
    """Инкремент live_* в meta.runtime (создаёт ключи с 0, если их ещё не было)."""
    runtime = dict(meta.get("runtime") or {})
    try:
        chars = int(runtime.get("live_history_chars") or 0)
    except (TypeError, ValueError):
        chars = 0
    try:
        count = int(runtime.get("live_msg_count") or 0)
    except (TypeError, ValueError):
        count = 0
    runtime["live_history_chars"] = max(0, chars + delta_chars)
    runtime["live_msg_count"] = max(0, count + delta_count)
    meta["runtime"] = runtime


def bump_live_volume_for_messages(
    meta: Dict[str, Any],
    messages: List[Dict[str, Any]],
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> None:
    """Инкремент live_* на объём только что дописанных payload."""
    bump_live_volume_in_meta(
        meta,
        delta_chars=calc_history_chars_for_trigger(
            messages, tags=tags, strip_think=strip_think
        ),
        delta_count=len(messages),
    )
