"""User-turn intent log callers (user_ask append, subagent seed)."""

from __future__ import annotations

from typing import Any

from gc_core.intent_log.operations import append_user_ask
from gc_core.intent_log.record import ACTION_USER_ASK
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)


def current_has_user_ask(current: list[Any], text: str) -> bool:
    line = str(text or "").strip()
    if not line:
        return False
    return any(
        record.action == ACTION_USER_ASK
        and str(record.content).strip() == line
        for record in current
    )


def append_user_ask_deduped(
    current: list[Any],
    text: str,
    *,
    user_turn_seq: int,
) -> list[Any]:
    line = str(text or "").strip()
    if not line or current_has_user_ask(current, line):
        return list(current)
    return append_user_ask(current, line, user_turn_seq=user_turn_seq)


def append_user_asks_from_messages(
    current: list[Any],
    messages: list[dict[str, Any]],
    *,
    user_turn_seq: int,
) -> list[Any]:
    out = list(current)
    for message in messages:
        if not isinstance(message, dict):
            continue
        text = extract_real_user_message_text(message)
        if text:
            out = append_user_ask_deduped(
                out, text, user_turn_seq=user_turn_seq
            )
    return out


def seed_subagent_current_user_intent(assignment_text: str) -> list[Any]:
    # Assignment — tool-arg parent agent'а; strip-tags живут в prose до tool_call, не здесь.
    line = str(assignment_text or "").strip()
    if not line:
        return []
    return append_user_ask([], line, user_turn_seq=0)


def init_subagent_open_turn(state: Any, assignment_text: str) -> None:
    """Subagent: empty closed log, open turn from assignment (no policy filter)."""
    state.user_intent_log = []
    state.user_turn_seq = 0
    state.finish_gate_rejection_count = 0
    init_notice = getattr(state, "init_subagent_tool_usage_notice", None)
    if callable(init_notice):
        init_notice()
    state.current_user_intent = seed_subagent_current_user_intent(
        assignment_text
    )
