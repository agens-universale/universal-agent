"""IntentRecord — structured event in user intent log."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

ACTION_USER_ASK = "user_ask"
ACTION_TOOL_AGGREGATED = "tool_result_aggregated"
ACTION_TOOL_RAW = "tool_result_raw"
ACTION_FINAL_ANSWER = "final_answer"

ALLOWED_ACTIONS = frozenset(
    {
        ACTION_USER_ASK,
        ACTION_TOOL_AGGREGATED,
        ACTION_TOOL_RAW,
        ACTION_FINAL_ANSWER,
    }
)


@dataclass(frozen=True)
class IntentRecord:
    turn: int
    ts: str
    action: str
    tool_name: str | None
    content: str | Mapping[str, Any]
    fallback: bool | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "turn": self.turn,
            "ts": self.ts,
            "action": self.action,
            "tool_name": self.tool_name,
            "content": self.content,
        }
        if self.fallback is not None:
            payload["fallback"] = self.fallback
        return payload


def validate_record(record: IntentRecord | Mapping[str, Any]) -> IntentRecord:
    """Normalize and validate a single intent log record."""
    if isinstance(record, IntentRecord):
        data = record.to_dict()
    elif isinstance(record, Mapping):
        data = dict(record)
    else:
        raise TypeError(f"Expected IntentRecord or mapping, got {type(record)!r}")

    action = str(data.get("action", "")).strip()
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"Invalid intent action: {action!r}")

    turn = data.get("turn")
    if not isinstance(turn, int) or turn < 0:
        raise ValueError(f"Invalid intent turn: {turn!r}")

    ts = str(data.get("ts", "")).strip()
    if not ts:
        raise ValueError("Intent record ts must be non-empty ISO UTC string")

    tool_name_raw = data.get("tool_name")
    tool_name = None if tool_name_raw is None else str(tool_name_raw).strip() or None
    content = data.get("content")
    fallback_raw = data.get("fallback")
    fallback = None if fallback_raw is None else bool(fallback_raw)

    if action == ACTION_USER_ASK:
        if tool_name is not None:
            raise ValueError("user_ask record must have tool_name=null")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("user_ask content must be a non-empty string")
        content = content.strip()
        fallback = None
    elif action == ACTION_TOOL_AGGREGATED:
        if not tool_name:
            raise ValueError("tool_result_aggregated requires tool_name")
        if not isinstance(content, str):
            raise ValueError("tool_result_aggregated content must be a string")
    elif action == ACTION_TOOL_RAW:
        if not tool_name:
            raise ValueError("tool_result_raw requires tool_name")
        if not isinstance(content, Mapping):
            raise ValueError("tool_result_raw content must be an object")
        fallback = None
    elif action == ACTION_FINAL_ANSWER:
        if tool_name is not None:
            raise ValueError("final_answer record must have tool_name=null")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("final_answer content must be a non-empty string")
        content = content.strip()
        fallback = None

    return IntentRecord(
        turn=turn,
        ts=ts,
        action=action,
        tool_name=tool_name,
        content=content,
        fallback=fallback,
    )


def record_from_dict(data: Mapping[str, Any]) -> IntentRecord:
    return validate_record(data)
