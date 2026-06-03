"""No-tool-call bounce: technical feedback when the agent emits prose without tools.

Pure functions — no state, no backend I/O. Callers wrap the returned text with
``wrap_technical_user_content`` and inject it as a synthetic user message.
"""
from __future__ import annotations

from typing import Any, Dict, List

NO_TOOL_CALL_BOUNCE_MESSAGE = (
    "There is no tool call in your response. The user does NOT see prose "
    "written outside tool calls — only tool calls render to the user. "
    "Retry with one of:\n"
    "  (1) If you intended an ACTION (read files, explore, search, write, etc.), "
    "call the appropriate tool NOW in this turn.\n"
    "  (2) If you intended to ASK THE USER (present options, get a decision), "
    "call the turn tool that asks the user (question=...) with a clear, self-contained question."
)


def extract_last_assistant_text(messages: List[Dict[str, Any]]) -> str:
    """
    Return concatenated text from the last assistant `message` in the batch.
    Returns empty string if there is no assistant message or the content is
    empty/whitespace-only. Handles both string content and list-of-parts.
    """
    if not messages:
        return ""
    for message in reversed(messages):
        if (
            isinstance(message, dict)
            and message.get("type") == "message"
            and message.get("role") == "assistant"
        ):
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts: List[str] = []
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text")
                        if isinstance(text, str) and text.strip():
                            parts.append(text)
                return "\n\n".join(parts).strip()
            return ""
    return ""


def format_no_tool_call_bounce(assistant_text: str) -> str:
    """
    Build the no-tool-call bounce message. If the assistant's just-emitted
    text is available, inline it in a copy-paste-ready block so the model
    doesn't have to recompose from memory. Otherwise fall back to the static
    NO_TOOL_CALL_BOUNCE_MESSAGE constant.
    """
    text = (assistant_text or "").strip()
    if not text:
        return NO_TOOL_CALL_BOUNCE_MESSAGE
    return (
        "There is no tool call in your response. Here is what you just "
        "wrote, which the user did NOT see (prose outside a tool call is "
        "invisible):\n\n"
        "<<<\n"
        f"{text}\n"
        ">>>\n\n"
        "Retry with one of:\n"
        "  (1) If you intended an ACTION (read files, explore, search, "
        "write, etc.), call the appropriate tool NOW in this turn.\n"
        "  (2) If you intended to ASK THE USER, call the turn ask-user tool (question=...). "
        "Reuse the relevant part of the block above VERBATIM where it is the "
        "content the user must react to — do not paraphrase or summarize it."
    )
