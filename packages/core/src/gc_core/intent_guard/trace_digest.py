from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gc_core.config_modules.quality_gate import digest_limits_from_params
from gc_core.config_modules.strip_tags_projection import projection_tags_from_config
from gc_core.message_format import dialog_history

from gc_core.constants.tool_names import FINISH_TOOL_NAME

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


def _without_last_turn_finish_call(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the last pending finish-turn tool_call from digest (§5.2)."""
    for idx in range(len(messages) - 1, -1, -1):
        if _is_turn_finish_call(messages[idx]):
            return messages[:idx] + messages[idx + 1 :]
    return list(messages)


def build_finish_gate_trace_digest(
    *,
    state: Any,
    cfg: dict | None = None,
    exclude_pending_turn_finish: bool = True,
) -> FinishGateTraceDigest:
    """
    ``list(state.messages)`` → trim → ``build_history_text`` (§5.2–5.3.1).

    When ``exclude_pending_turn_finish`` is true, omit the in-flight finish handoff call
    so the validator compares the report to prior trace only.

    Axis A intent lives in ``state.current_user_intent``; not duplicated in the digest.
    """
    history_messages = [
        m for m in (getattr(state, "messages", None) or []) if isinstance(m, dict)
    ]
    if exclude_pending_turn_finish:
        history_messages = _without_last_turn_finish_call(history_messages)

    qg_params = None
    if isinstance(cfg, dict) and isinstance(cfg.get("quality_gate_params"), dict):
        qg_params = cfg["quality_gate_params"]
    limits = digest_limits_from_params(qg_params)
    tags = projection_tags_from_config(cfg)
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
