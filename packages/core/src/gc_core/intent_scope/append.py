"""Post-tool intent log append (aggregated / raw) — caller layer, not State."""

from __future__ import annotations

from typing import Any

from gc_core.intent_log import (
    ACTION_FINAL_ANSWER,
    ACTION_TOOL_AGGREGATED,
    ACTION_TOOL_RAW,
    append_final_answer,
    append_tool_aggregated,
    append_tool_raw,
)


def _current_has_aggregated(
    current: list[Any],
    tool_name: str,
    content: str,
    *,
    fallback: bool = False,
) -> bool:
    name = str(tool_name or "").strip()
    body = str(content or "")
    for record in current:
        if (
            record.action == ACTION_TOOL_AGGREGATED
            and record.tool_name == name
            and str(record.content) == body
            and bool(record.fallback) == fallback
        ):
            return True
    return False


def _current_has_raw(
    current: list[Any],
    tool_name: str,
    arguments: dict[str, Any] | None,
    output: Any,
) -> bool:
    name = str(tool_name or "").strip()
    expected_args = None if arguments is None else dict(arguments)
    return any(
        _raw_record_matches(record, name, expected_args, output) for record in current
    )


def _raw_record_matches(record: Any, name: str, expected_args: dict | None, output: Any) -> bool:
    if record.action != ACTION_TOOL_RAW or record.tool_name != name:
        return False
    if not isinstance(record.content, dict):
        return False
    if expected_args is None:
        if "arguments" in record.content:
            return False
    elif dict(record.content.get("arguments") or {}) != expected_args:
        return False
    return record.content.get("output") == output


def append_tool_aggregated_to_current(
    state: Any,
    tool_name: str,
    content: str,
    *,
    fallback: bool = False,
) -> bool:
    """Append aggregated tool result to ``state.current_user_intent``. Returns True if appended."""
    name = str(tool_name or "").strip()
    body = str(content or "")
    if not name or not body.strip():
        return False
    current = list(getattr(state, "current_user_intent", []) or [])
    if _current_has_aggregated(current, name, body, fallback=fallback):
        return False
    state.current_user_intent = append_tool_aggregated(
        current,
        name,
        body,
        user_turn_seq=int(getattr(state, "user_turn_seq", 0)),
        fallback=fallback,
    )
    return True


def append_tool_raw_to_current(
    state: Any,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    output: Any = None,
) -> bool:
    """Append raw tool result to ``state.current_user_intent``. Returns True if appended."""
    name = str(tool_name or "").strip()
    if not name:
        return False
    args = None if arguments is None else dict(arguments)
    current = list(getattr(state, "current_user_intent", []) or [])
    if _current_has_raw(current, name, args, output):
        return False
    state.current_user_intent = append_tool_raw(
        current,
        name,
        args,
        output,
        user_turn_seq=int(getattr(state, "user_turn_seq", 0)),
    )
    return True


def _current_has_final_answer(current: list[Any], content: str) -> bool:
    body = str(content or "")
    return any(record.action == ACTION_FINAL_ANSWER and str(record.content) == body for record in current)


def append_final_answer_to_current(state: Any, content: str) -> bool:
    """Append final prose answer to ``state.current_user_intent``. Returns True if appended."""
    body = str(content or "").strip()
    if not body:
        return False
    current = list(getattr(state, "current_user_intent", []) or [])
    if _current_has_final_answer(current, body):
        return False
    state.current_user_intent = append_final_answer(
        current, body, user_turn_seq=int(getattr(state, "user_turn_seq", 0))
    )
    return True
