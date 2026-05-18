from __future__ import annotations

from copy import deepcopy
from typing import Dict, List


def _collect_tool_output_call_id(message: Dict) -> str | None:
    role = message.get("role")
    if role == "tool":
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            return tool_call_id
    if message.get("type") == "function_call_output":
        call_id = message.get("call_id")
        if isinstance(call_id, str) and call_id:
            return call_id
    return None


def repair_tool_call_pairing(
    messages: List[Dict],
    *,
    keep_latest_unanswered: bool = True,
) -> List[Dict]:
    """
    Keep assistant.tool_calls only when each call has a matching tool reply
    (role=tool, tool_call_id=...) later in the history.
    """
    if not isinstance(messages, list) or not messages:
        return messages

    future_tool_call_ids: set[str] = set()
    normalized_reversed: List[Dict] = []

    for message in reversed(messages):
        if not isinstance(message, dict):
            continue

        call_output_id = _collect_tool_output_call_id(message)
        if call_output_id:
            future_tool_call_ids.add(call_output_id)
            normalized_reversed.append(message)
            continue
        role = message.get("role")

        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            tool_calls = message.get("tool_calls", [])
            # Optionally keep unresolved trailing assistant tool_calls as-is.
            if keep_latest_unanswered and not future_tool_call_ids and not normalized_reversed:
                normalized_reversed.append(message)
                continue

            filtered_tool_calls = []
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                call_id = tool_call.get("id")
                if isinstance(call_id, str) and call_id in future_tool_call_ids:
                    filtered_tool_calls.append(tool_call)

            updated = deepcopy(message)
            if filtered_tool_calls:
                updated["tool_calls"] = filtered_tool_calls
            else:
                updated.pop("tool_calls", None)
                content = updated.get("content")
                if content in ("", None, []):
                    continue
            normalized_reversed.append(updated)
            continue

        if message.get("type") == "function_call":
            call_id = message.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            # Optionally keep unresolved trailing function_call as-is.
            if keep_latest_unanswered and not future_tool_call_ids and not normalized_reversed:
                normalized_reversed.append(message)
                continue
            if call_id in future_tool_call_ids:
                normalized_reversed.append(message)
            continue

        normalized_reversed.append(message)

    normalized_reversed.reverse()
    return normalized_reversed
