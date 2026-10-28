"""Агрегаты объёма live-окна в meta.runtime (persist-owned).

Ключи ``live_history_chars`` / ``live_msg_count`` пишет thread_store при
append / compact / discard; TUI только читает (см. docs_md/tui/meta-ownership).
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from gc_core.message_format import calc_history_chars_for_trigger


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
