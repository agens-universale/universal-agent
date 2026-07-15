"""Агрегаты объёма live-окна в meta.tui (persist-owned).

Ключи ``live_history_chars`` / ``live_msg_count`` пишет thread_store при
append / compact / discard; TUI только читает (см. docs_md/tui/context-meter-poll).
"""
from __future__ import annotations

from typing import Any, Dict, List

from message_format import calc_history_chars_for_trigger


def merge_tui_into_meta(meta: Dict[str, Any], patch: Dict[str, Any]) -> None:
    """Слить patch в meta['tui'] in-place (не затирая чужие ключи)."""
    tui = dict(meta.get("tui") or {})
    tui.update(patch)
    meta["tui"] = tui


def live_volume_from_messages(messages: List[Dict[str, Any]]) -> Dict[str, int]:
    """Полный пересчёт агрегатов объёма live-окна."""
    return {
        "live_history_chars": calc_history_chars_for_trigger(messages),
        "live_msg_count": len(messages),
    }


def bump_live_volume_in_meta(
    meta: Dict[str, Any], *, delta_chars: int, delta_count: int
) -> None:
    """Инкремент live_* в meta (создаёт ключи с 0, если их ещё не было)."""
    tui = dict(meta.get("tui") or {})
    try:
        chars = int(tui.get("live_history_chars") or 0)
    except (TypeError, ValueError):
        chars = 0
    try:
        count = int(tui.get("live_msg_count") or 0)
    except (TypeError, ValueError):
        count = 0
    tui["live_history_chars"] = max(0, chars + delta_chars)
    tui["live_msg_count"] = max(0, count + delta_count)
    meta["tui"] = tui


def bump_live_volume_for_messages(
    meta: Dict[str, Any], messages: List[Dict[str, Any]]
) -> None:
    """Инкремент live_* на объём только что дописанных payload."""
    bump_live_volume_in_meta(
        meta,
        delta_chars=calc_history_chars_for_trigger(messages),
        delta_count=len(messages),
    )
