"""No-tool-call bounce: technical feedback when the agent emits prose without tools.

Pure functions — no state, no backend I/O. Callers wrap the returned text with
``wrap_technical_user_content`` and inject it as a synthetic user message.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from gc_core.message_projection import project_assistant_text
from gc_core.utils.backend_errors import (
    FINISH_KIND_BLOCKED,
    FINISH_KIND_LENGTH,
    classify_finish_kind,
    finish_blocks_final,
)

NO_TOOL_CALL_BOUNCE_MESSAGE = (
    "There is no tool call in your response. "
    "Choose a tool call that brings you closer to solving the task."
)

PROVIDER_BLOCKED_BOUNCE_MESSAGE = (
    "The provider blocked this response (content_filter/refusal/safety). "
    "Rephrase: avoid the blocked wording, take a different approach, "
    "and continue with a tool call that brings you closer to solving the task."
)

# Alias: историческое имя; то же сообщение для любого blocked finish.
CONTENT_FILTER_BOUNCE_MESSAGE = PROVIDER_BLOCKED_BOUNCE_MESSAGE

LENGTH_BOUNCE_MESSAGE = (
    "Your previous response was truncated (finish_reason=length). "
    "Shorten the next reply or split work into smaller tool calls "
    "that bring you closer to solving the task."
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


def is_content_filter_finish(finish_reason: str | None) -> bool:
    """True для любого provider-blocked finish (content_filter/refusal/safety)."""
    return finish_blocks_final(finish_reason)


def format_no_tool_call_bounce(
    assistant_text: str = "",
    *,
    finish_reason: str | None = None,
) -> str:
    """Return the static no-tool-call nudge.

    Assistant prose is already in history (committed before the bounce), so
    it is not inlined here. Kind comes from ``classify_finish_kind``:
    blocked → rewrite nudge; length → truncation nudge; else generic tool call.
    """
    del assistant_text
    kind = classify_finish_kind(finish_reason)
    if kind == FINISH_KIND_BLOCKED:
        return PROVIDER_BLOCKED_BOUNCE_MESSAGE
    if kind == FINISH_KIND_LENGTH:
        return LENGTH_BOUNCE_MESSAGE
    return NO_TOOL_CALL_BOUNCE_MESSAGE
