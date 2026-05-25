"""Yandex Cloud OAI-compatible endpoint: pre- and post-call quirks.

Two issues handled here:

1. Pre-call — rejects messages where ``content`` is ``""`` / ``None`` /
   ``[]`` with HTTP 400 ``empty message text``. We coerce to a single space.

2. Post-call — Yandex has no native LiteLLM provider, so multi-turn tool
   calling breaks: it returns the chosen tool's arguments as a JSON string
   inside ``message.content`` (often wrapped in ```` ```...``` `` fences)
   instead of the OpenAI ``tool_calls`` array. We synthesize the missing
   ``tool_calls`` post-call by matching parsed JSON keys against declared
   tool schemas.
"""
import uuid
from typing import Any

from callbacks._common import (
    detect_tool_call_from_payload,
    get_message_field,
    get_message_from_choice,
    iter_response_choices,
    parse_json_object,
    set_choice_finish_reason,
    set_message_tool_call,
)


def backfill_empty_content(messages: list[dict]) -> int:
    """Ensure every message has non-empty ``content``.

    Yandex Cloud's OAI-compat endpoint rejects with HTTP 400 if a message
    has any of: missing ``content`` key, ``None`` / ``""`` / ``[]``, or a
    multimodal text part with empty ``text``. All these are valid OpenAI
    shapes; we coerce to a single space.

    Returns the number of messages that were patched (for logging).
    """
    patched = 0
    for msg in messages:
        c = msg.get("content")
        if c is None or c == "" or c == []:
            msg["content"] = " "
            patched += 1
            continue
        if isinstance(c, list):
            for part in c:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and not part.get("text")
                ):
                    part["text"] = " "
                    patched += 1
    return patched


def synthesize_tool_calls(response: Any, tools: list[dict]) -> int:
    """Walk response choices; for any assistant message that has plain text
    ``content`` matching a known tool's argument schema, synthesize the
    ``tool_calls`` array and clear ``content``.

    Returns the number of choices patched.
    """
    if not tools:
        return 0

    patched = 0
    for choice in iter_response_choices(response):
        message = get_message_from_choice(choice)
        if message is None:
            continue

        if get_message_field(message, "tool_calls"):
            continue

        content = get_message_field(message, "content")
        if not isinstance(content, str) or not content.strip():
            continue

        payload = parse_json_object(content)
        if payload is None:
            continue

        match = detect_tool_call_from_payload(payload, tools)
        if match is None:
            continue
        tool_name, args_str = match

        synthetic_call = {
            "id": f"call_yandex_{uuid.uuid4().hex[:16]}",
            "type": "function",
            "function": {"name": tool_name, "arguments": args_str},
        }

        if not set_message_tool_call(message, synthetic_call):
            continue
        set_choice_finish_reason(choice, "tool_calls")
        patched += 1
    return patched
