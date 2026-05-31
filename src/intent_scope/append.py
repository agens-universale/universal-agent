"""Post-tool intent log append (aggregated / raw) — caller layer, not State."""

from __future__ import annotations

from typing import Any

from intent_log import (
    ACTION_TOOL_AGGREGATED,
    ACTION_TOOL_RAW,
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
    arguments: dict[str, Any],
    output: Any,
) -> bool:
    name = str(tool_name or "").strip()
    for record in current:
        if record.action != ACTION_TOOL_RAW or record.tool_name != name:
            continue
        if not isinstance(record.content, dict):
            continue
        if dict(record.content.get("arguments") or {}) != dict(arguments):
            continue
        if record.content.get("output") != output:
            continue
        return True
    return False


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
    arguments: dict[str, Any],
    output: Any = None,
) -> bool:
    """Append raw tool result to ``state.current_user_intent``. Returns True if appended."""
    name = str(tool_name or "").strip()
    if not name:
        return False
    args = dict(arguments or {})
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
