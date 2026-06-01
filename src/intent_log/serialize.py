"""JSONL serialization for intent log records."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from intent_log.record import (
    ACTION_TOOL_AGGREGATED,
    ACTION_TOOL_RAW,
    ACTION_USER_ASK,
    IntentRecord,
    record_from_dict,
    validate_record,
)

# Placeholder ts for legacy meta lines (`user_intent_log: list[str]`).
LEGACY_LOAD_TS = "1970-01-01T00:00:00Z"


def serialize(records: Iterable[IntentRecord | Mapping[str, Any]]) -> str:
    """Serialize records to JSONL text (one JSON object per line)."""
    lines: list[str] = []
    for item in records:
        record = validate_record(item)
        lines.append(json.dumps(record.to_dict(), ensure_ascii=False))
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _legacy_string_to_record(text: str, *, turn: int = 1) -> IntentRecord | None:
    """Best-effort: old meta stored intent lines as plain strings."""
    line = str(text or "").strip()
    if not line:
        return None
    if line.startswith("[intent_scope:raw] "):
        body = line[len("[intent_scope:raw] ") :].strip()
        tool_name = "unknown"
        content: str | dict[str, Any] = body
        if ": " in body:
            tool_name, rest = body.split(": ", 1)
            tool_name = tool_name.strip() or "unknown"
            try:
                parsed = json.loads(rest)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                content = parsed if "arguments" in parsed else {"arguments": parsed}
            else:
                content = {"arguments": {}, "output": rest}
        return validate_record(
            IntentRecord(
                turn=turn,
                ts=LEGACY_LOAD_TS,
                action=ACTION_TOOL_RAW,
                tool_name=tool_name,
                content=content,
            )
        )
    if line.startswith("[") and "] " in line:
        prefix, content = line.split("] ", 1)
        tool_name = prefix.lstrip("[").strip() or "unknown"
        fallback = "[fallback]" in tool_name
        tool_name = tool_name.replace(" [fallback]", "").strip() or "unknown"
        return validate_record(
            IntentRecord(
                turn=turn,
                ts=LEGACY_LOAD_TS,
                action=ACTION_TOOL_AGGREGATED,
                tool_name=tool_name,
                content=content,
                fallback=True if fallback else None,
            )
        )
    return validate_record(
        IntentRecord(
            turn=turn,
            ts=LEGACY_LOAD_TS,
            action=ACTION_USER_ASK,
            tool_name=None,
            content=line,
        )
    )


def deserialize_list(items: Iterable[Any] | None) -> list[IntentRecord]:
    """Load structured records from persisted meta list.

    Accepts legacy ``list[str]`` meta (load-only); next persist rewrites structured records.
    """
    if not items:
        return []
    out: list[IntentRecord] = []
    for item in items:
        if isinstance(item, IntentRecord):
            out.append(validate_record(item))
            continue
        if isinstance(item, Mapping):
            out.append(record_from_dict(item))
            continue
        if isinstance(item, str):
            record = _legacy_string_to_record(item)
            if record is not None:
                out.append(record)
            continue
        raise TypeError(f"Cannot deserialize intent record from {type(item)!r}")
    return out


def serialize_for_meta(records: Iterable[IntentRecord | Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Persist intent log as structured list in thread meta (not JSONL file)."""
    return [validate_record(item).to_dict() for item in records]


def records_to_jsonl_lines(records: Iterable[IntentRecord | Mapping[str, Any]]) -> list[str]:
    """One canonical JSONL line per IntentRecord (§2.1)."""
    text = serialize(records)
    if not text:
        return []
    return text.splitlines()
