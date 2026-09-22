"""Test helpers for structured intent log (IntentRecord)."""

from __future__ import annotations

import json

from gc_core.intent_log import (
    ACTION_USER_ASK,
    IntentRecord,
    append_user_ask,
    records_to_jsonl_lines,
    serialize_for_meta,
)

TS = "2026-05-30T11:35:08Z"


def user_ask_record(content: str, *, turn: int = 1) -> IntentRecord:
    return IntentRecord(
        turn=turn,
        ts=TS,
        action=ACTION_USER_ASK,
        tool_name=None,
        content=content,
    )


def user_ask_records(*contents: str, turn: int = 1) -> list[IntentRecord]:
    return [user_ask_record(content, turn=turn) for content in contents]


def meta_intent_log(*contents: str, turn: int = 1) -> list[dict]:
    return serialize_for_meta(user_ask_records(*contents, turn=turn))


def intent_user_ask_texts(records) -> list[str]:
    if not records:
        return []
    return [str(record.content) for record in records if getattr(record, "action", None) == ACTION_USER_ASK]


def intent_jsonl_lines(records) -> list[str]:
    """Canonical JSONL wire lines for finish gate / validator payloads."""
    return records_to_jsonl_lines(records)


def intent_user_ask_texts_from_jsonl(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines or []:
        if not isinstance(line, str) or not line.strip():
            continue
        obj = json.loads(line)
        if obj.get("action") == ACTION_USER_ASK:
            out.append(str(obj.get("content") or ""))
    return out


def seed_state_intent(
    state,
    *,
    current: str | list[str] | None = None,
    log: str | list[str] | None = None,
) -> None:
    state.user_turn_seq = 0
    state.user_intent_log = []
    state.current_user_intent = []
    log_items = [log] if isinstance(log, str) else list(log or [])
    for text in log_items:
        state.current_user_intent = append_user_ask(
            [],
            text,
            user_turn_seq=state.user_turn_seq,
            ts=TS,
        )
        state.flush_current_user_intent()
    current_items = [current] if isinstance(current, str) else list(current or [])
    for text in current_items:
        state.current_user_intent = append_user_ask(
            state.current_user_intent,
            text,
            user_turn_seq=state.user_turn_seq,
            ts=TS,
        )
