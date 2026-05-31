"""Append, flush, pagination and legacy migration for intent log."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from intent_log.record import (
    ACTION_TOOL_AGGREGATED,
    ACTION_TOOL_RAW,
    ACTION_USER_ASK,
    IntentRecord,
    validate_record,
)
from intent_log.serialize import deserialize_list, serialize


def utc_now_ts() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _open_turn(user_turn_seq: int) -> int:
    return int(user_turn_seq) + 1


def append_user_ask(
    current: list[IntentRecord],
    text: str,
    *,
    user_turn_seq: int,
    ts: str | None = None,
) -> list[IntentRecord]:
    line = str(text or "").strip()
    if not line:
        return list(current)
    record = validate_record(
        IntentRecord(
            turn=_open_turn(user_turn_seq),
            ts=ts or utc_now_ts(),
            action=ACTION_USER_ASK,
            tool_name=None,
            content=line,
        )
    )
    return [*current, record]


def append_tool_aggregated(
    current: list[IntentRecord],
    tool_name: str,
    content: str,
    *,
    user_turn_seq: int,
    fallback: bool = False,
    ts: str | None = None,
) -> list[IntentRecord]:
    name = str(tool_name or "").strip()
    if not name:
        raise ValueError("tool_name is required for aggregated tool result")
    record = validate_record(
        IntentRecord(
            turn=_open_turn(user_turn_seq),
            ts=ts or utc_now_ts(),
            action=ACTION_TOOL_AGGREGATED,
            tool_name=name,
            content=str(content or ""),
            fallback=True if fallback else None,
        )
    )
    return [*current, record]


def append_tool_raw(
    current: list[IntentRecord],
    tool_name: str,
    arguments: Mapping[str, Any],
    output: Any = None,
    *,
    user_turn_seq: int,
    ts: str | None = None,
) -> list[IntentRecord]:
    name = str(tool_name or "").strip()
    if not name:
        raise ValueError("tool_name is required for raw tool result")
    if not isinstance(arguments, Mapping):
        raise TypeError("arguments must be a mapping")
    content: dict[str, Any] = {"arguments": dict(arguments)}
    if output is not None:
        content["output"] = output
    record = validate_record(
        IntentRecord(
            turn=_open_turn(user_turn_seq),
            ts=ts or utc_now_ts(),
            action=ACTION_TOOL_RAW,
            tool_name=name,
            content=content,
        )
    )
    return [*current, record]


def flush(
    current: list[IntentRecord],
    log: list[IntentRecord],
    user_turn_seq: int,
) -> tuple[list[IntentRecord], list[IntentRecord], int]:
    """Move open turn records to closed log and increment turn counter."""
    closed = [validate_record(r) for r in current]
    new_log = [*log, *closed]
    return [], new_log, int(user_turn_seq) + 1


def read_page(
    records: list[IntentRecord],
    offset: int,
    limit: int,
    *,
    default_limit: int,
) -> dict[str, Any]:
    """Char-offset slice over serialized closed log (read tool semantics)."""
    text = serialize(records)
    total_chars = len(text)
    total_records = len(records)
    safe_offset = max(0, int(offset))
    safe_limit = max(0, int(limit))
    content = text[safe_offset : safe_offset + safe_limit]
    next_offset = safe_offset + len(content)
    truncated = next_offset < total_chars
    return {
        "total_chars": total_chars,
        "total_records": total_records,
        "default_limit": int(default_limit),
        "offset": safe_offset,
        "limit": safe_limit,
        "content": content,
        "next_offset": next_offset,
        "truncated": truncated,
    }


def read_info(
    records: list[IntentRecord],
    *,
    default_limit: int,
) -> dict[str, Any]:
    text = serialize(records)
    return {
        "total_chars": len(text),
        "total_records": len(records),
        "default_limit": int(default_limit),
    }


def tail_preview(
    records: list[IntentRecord],
    max_chars: int,
) -> dict[str, Any]:
    """Tail slice for summary preview (same offset/limit contract as read_page)."""
    text = serialize(records)
    total_chars = len(text)
    total_records = len(records)
    safe_max = max(0, int(max_chars))
    if total_chars <= safe_max:
        return {
            "offset": 0,
            "limit": safe_max,
            "content": text,
            "truncated": False,
            "total_chars": total_chars,
            "total_records": total_records,
        }
    offset = total_chars - safe_max
    return {
        "offset": offset,
        "limit": safe_max,
        "content": text[offset:],
        "truncated": True,
        "total_chars": total_chars,
        "total_records": total_records,
    }
