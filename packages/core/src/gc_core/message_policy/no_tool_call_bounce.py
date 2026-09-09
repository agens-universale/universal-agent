"""No-tool-call bounce: technical feedback when the agent emits prose without tools.

Pure functions — no state, no backend I/O. Callers wrap the returned text with
``wrap_technical_user_content`` and inject it as a synthetic user message.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from gc_core.message_projection import project_assistant_text

NO_TOOL_CALL_BOUNCE_MESSAGE = (
    "There is no tool call in your response. "
    "Choose a tool call that brings you closer to solving the task."
)


def extract_last_assistant_text(
    messages: List[Dict[str, Any]],
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> str:
    """
    Return projected prose from the last assistant `message` in the batch
    (``for_model``: ``<think>`` stripped when ``strip_think=True``). Empty
    string if missing or empty after projection. Handles string content
    and list-of-parts.
    """
    if not messages:
        return ""
    for message in reversed(messages):
        if (
            isinstance(message, dict)
            and message.get("type") == "message"
            and message.get("role") == "assistant"
        ):
            return project_assistant_text(
                message.get("content"), tags=tags, strip_think=strip_think
            ).strip()
    return ""


def format_no_tool_call_bounce(assistant_text: str = "") -> str:
    """Return the static no-tool-call nudge.

    Assistant prose is already in history (committed before the bounce), so
    it is not inlined here.
    """
    del assistant_text
    return NO_TOOL_CALL_BOUNCE_MESSAGE
