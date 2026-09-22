"""Append, flush, pagination and legacy migration for intent log."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping

from gc_core.intent_log.record import (
    ACTION_FINAL_ANSWER,
    ACTION_TOOL_AGGREGATED,
    ACTION_TOOL_RAW,
    ACTION_USER_ASK,
    IntentRecord,
    validate_record,
)
from gc_core.intent_log.serialize import serialize


def utc_now_ts() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    arguments: Mapping[str, Any] | None = None,
    output: Any = None,
    *,
    user_turn_seq: int,
    ts: str | None = None,
) -> list[IntentRecord]:
    name = str(tool_name or "").strip()
    if not name:
        raise ValueError("tool_name is required for raw tool result")
    if arguments is None and output is None:
        raise ValueError("append_tool_raw requires at least one of arguments/output")
    content: dict[str, Any] = {}
    if arguments is not None:
        if not isinstance(arguments, Mapping):
            raise TypeError("arguments must be a mapping when provided")
        content["arguments"] = dict(arguments)
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


def append_final_answer(
    current: list[IntentRecord],
    content: str,
    *,
    user_turn_seq: int,
    ts: str | None = None,
) -> list[IntentRecord]:
    body = str(content or "").strip()
    if not body:
        return list(current)
    record = validate_record(
        IntentRecord(
            turn=_open_turn(user_turn_seq),
            ts=ts or utc_now_ts(),
            action=ACTION_FINAL_ANSWER,
            tool_name=None,
            content=body,
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


def _log_totals(records: list[IntentRecord]) -> tuple[int, int]:
    text = serialize(records)
    return len(records), len(text)


def _suffix_records_within_char_budget(
    records: list[IntentRecord],
    max_chars: int,
) -> tuple[list[IntentRecord], int]:
    """Largest suffix of ``records`` whose JSONL fits in ``max_chars`` (whole records only)."""
    total = len(records)
    if total == 0:
        return [], 0
    safe_max = max(0, int(max_chars))
    if safe_max == 0:
        return [], total

    best_start = total
    for start in range(total):
        chunk = records[start:]
        if len(serialize(chunk)) <= safe_max:
            best_start = start
            break

    if best_start < total:
        return records[best_start:], best_start

    # Single record exceeds budget — still return the last record whole.
    return records[-1:], total - 1


def read_page(
    records: list[IntentRecord],
    offset: int,
    limit: int,
    *,
    default_limit: int,
) -> dict[str, Any]:
    """Paginate closed intent log by **record index** (``read_user_intent_log``).

    ``offset`` and ``limit`` are counts of ``IntentRecord`` entries, not characters.
    Page slice: ``records[offset : offset + limit]``; ``content`` is JSONL of that slice
    (always whole lines).

    Returned dict:
    - ``offset``, ``limit``, ``next_offset`` — record indices (0-based);
    - ``total_records``, ``total_chars`` — full closed log;
    - ``default_limit`` — suggested ``limit`` when caller omits it (from config);
    - ``page_records``, ``page_chars`` — size of this page;
    - ``truncated`` — ``True`` if more records exist after this page.
    """
    total_records, total_chars = _log_totals(records)
    safe_offset = max(0, int(offset))
    if safe_offset > total_records:
        safe_offset = total_records
    safe_limit = max(0, int(limit))
    page_records = records[safe_offset : safe_offset + safe_limit]
    content = serialize(page_records)
    next_offset = safe_offset + len(page_records)
    truncated = next_offset < total_records
    return {
        "total_chars": total_chars,
        "total_records": total_records,
        "default_limit": int(default_limit),
        "offset": safe_offset,
        "limit": safe_limit,
        "content": content,
        "next_offset": next_offset,
        "truncated": truncated,
        "page_chars": len(content),
        "page_records": len(page_records),
    }


def read_info(
    records: list[IntentRecord],
    *,
    default_limit: int,
) -> dict[str, Any]:
    """Totals for closed log; ``default_limit`` is records-per-page hint (not chars)."""
    total_records, total_chars = _log_totals(records)
    return {
        "total_chars": total_chars,
        "total_records": total_records,
        "default_limit": int(default_limit),
    }


def tail_preview(
    records: list[IntentRecord],
    max_chars: int,
) -> dict[str, Any]:
    """Tail slice for dialog_summary: last records whose JSONL fits in ``max_chars``.

    ``max_chars`` is a **character** budget; ``offset`` / ``limit`` in the result are
    **record** index and count (same meaning as :func:`read_page`), not char positions.
    """
    total_records, total_chars = _log_totals(records)
    tail_records, record_offset = _suffix_records_within_char_budget(records, max_chars)
    content = serialize(tail_records)
    page_records = len(tail_records)
    truncated = record_offset > 0
    return {
        "offset": record_offset,
        "limit": page_records,
        "content": content,
        "truncated": truncated,
        "total_chars": total_chars,
        "total_records": total_records,
        "page_chars": len(content),
        "page_records": page_records,
    }
