from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gc_core.constants.tool_names import FINISH_TOOL_NAME
from gc_core.message_format import dialog_history
from gc_core.settings import settings_of

_FINISH_TOOL_NAMES = frozenset({FINISH_TOOL_NAME})


@dataclass(frozen=True)
class FinishGateTraceDigest:
    trace_digest_text: str
    digest_chars: int


def _is_turn_finish_call(message: dict[str, Any]) -> bool:
    if not isinstance(message, dict):
        return False
    msg_type = str(message.get("type") or "message")
    if msg_type not in ("function_call", "tool_call"):
        return False
    return str(message.get("name") or "") in _FINISH_TOOL_NAMES


def _without_last_turn_finish_call(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop the pending finish call and its output from the judge digest (§5.2)."""
    for idx in range(len(messages) - 1, -1, -1):
        if _is_turn_finish_call(messages[idx]):
            call_id = messages[idx].get("call_id")
            return [
                message
                for pos, message in enumerate(messages)
                if pos != idx
                and not (
                    message.get("type") == "function_call_output"
                    and message.get("call_id") == call_id
                )
            ]
    return list(messages)


def build_finish_gate_trace_digest(
    *,
    state: Any,
    settings: Any = None,
    exclude_pending_turn_finish: bool = True,
) -> FinishGateTraceDigest:
    """
    ``list(state.messages)`` → trim → ``build_history_text`` (§5.2–5.3.1).

    When ``exclude_pending_turn_finish`` is true, omit the in-flight finish handoff call
    so the validator compares the report to prior trace only.

    Axis A intent lives in ``state.current_user_intent``; not duplicated in the digest.
    """
    history_messages = [
        m
        for m in (getattr(state, "messages", None) or [])
        if isinstance(m, dict)
    ]
    if exclude_pending_turn_finish:
        history_messages = _without_last_turn_finish_call(history_messages)

    # settings — то, что принимает settings_of (ctx, LoadedSettings,
    # AppConfig); None — дефолты модели.
    settings = settings_of(settings)
    limits = {
        "max_dialogue_chars": int(settings.quality_gate.max_dialogue_chars),
        "max_message_chars": int(settings.quality_gate.max_message_chars),
    }
    tags = tuple(str(tag) for tag in settings.strip_tags_projection.tags)
    trimmed = dialog_history.trim_history_to_max_dialogue_chars(
        history_messages,
        max_dialogue_chars=limits["max_dialogue_chars"],
        max_message_chars=limits["max_message_chars"],
        tags=tags,
        strip_think=True,
    )
    trace_digest_text = dialog_history.build_history_text(
        trimmed,
        max_chars=0,
        max_message_chars=limits["max_message_chars"],
        tags=tags,
        strip_think=True,
    )
    return FinishGateTraceDigest(
        trace_digest_text=trace_digest_text,
        digest_chars=len(trace_digest_text),
    )
