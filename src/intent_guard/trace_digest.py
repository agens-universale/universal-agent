from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config_modules.quality_gate import digest_limits_from_params
from message_format import dialog_history

_SUBMIT_SOLUTION_TOOL_NAMES = frozenset({"submit_solution"})


@dataclass(frozen=True)
class FinishGateTraceDigest:
    trace_digest_text: str
    digest_chars: int


def _is_submit_solution_call(message: dict[str, Any]) -> bool:
    if not isinstance(message, dict):
        return False
    msg_type = str(message.get("type") or "message")
    if msg_type not in ("function_call", "tool_call"):
        return False
    return str(message.get("name") or "") in _SUBMIT_SOLUTION_TOOL_NAMES


def _without_last_submit_solution_call(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Убрать последний pending ``submit_solution`` (§5.2)."""
    for idx in range(len(messages) - 1, -1, -1):
        if _is_submit_solution_call(messages[idx]):
            return messages[:idx] + messages[idx + 1 :]
    return list(messages)


def build_finish_gate_trace_digest(
    *,
    state: Any,
    cfg: dict | None = None,
    exclude_pending_submit_solution: bool = True,
) -> FinishGateTraceDigest:
    """
    ``list(state.messages)`` → trim → ``build_history_text`` (§5.2–5.3.1).

    Intent для оси A — отдельно из ``state.current_user_messages``; в digest не дублируется.
    """
    history_messages = [
        m for m in (getattr(state, "messages", None) or []) if isinstance(m, dict)
    ]
    if exclude_pending_submit_solution:
        history_messages = _without_last_submit_solution_call(history_messages)

    qg_params = None
    if isinstance(cfg, dict) and isinstance(cfg.get("quality_gate_params"), dict):
        qg_params = cfg["quality_gate_params"]
    limits = digest_limits_from_params(qg_params)
    trimmed = dialog_history.trim_history_to_max_dialogue_chars(
        history_messages,
        max_dialogue_chars=limits["max_dialogue_chars"],
        max_message_chars=limits["max_message_chars"],
    )
    trace_digest_text = dialog_history.build_history_text(
        trimmed,
        max_chars=0,
        max_message_chars=limits["max_message_chars"],
    )
    return FinishGateTraceDigest(
        trace_digest_text=trace_digest_text,
        digest_chars=len(trace_digest_text),
    )
