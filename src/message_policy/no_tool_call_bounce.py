"""No-tool-call bounce: technical feedback when the agent emits prose without tools.

Pure functions — no state, no backend I/O. Callers wrap the returned text with
``wrap_technical_user_content`` and inject it as a synthetic user message.
"""
from __future__ import annotations

from typing import Any, Dict, List

NO_TOOL_CALL_BOUNCE_MESSAGE = (
    "There is no tool call in your response. This runtime's contract is that "
    "every turn must contain at least one appropriate tool call — satisfy it now:\n"
    "  - If the work for this turn is done or you are blocked, finalize the turn: "
    "use the designated user-output/finalization tool (it delivers your reply to "
    "the user).\n"
    "  - If you are not finished, call the tool for your next step — the tool that "
    "asks the user / waits for input (one precise question), or an action tool "
    "(read, search, write, run, etc.)."
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
        "There is no tool call in your response. This runtime's contract is that "
        "every turn must contain at least one appropriate tool call; the prose you "
        "just wrote was not a tool call, so it did not reach the user:\n\n"
        "<<<\n"
        f"{text}\n"
        ">>>\n\n"
        "Satisfy the contract now:\n"
        "  - If the work for this turn is done or you are blocked, finalize the "
        "turn: use the designated user-output/finalization tool (it delivers your "
        "reply to the user).\n"
        "  - If you are not finished, call the tool for your next step — the tool "
        "that asks the user / waits for input (one precise question), or an action "
        "tool (read, search, write, run, etc.).\n"
        "Reuse the relevant part of the block above VERBATIM where it is the "
        "content the user must react to — do not paraphrase or summarize it."
    )
