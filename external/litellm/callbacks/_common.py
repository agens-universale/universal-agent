"""Helpers shared between vendor-specific callback modules.

Stays vendor-agnostic — anything touching a specific provider name lives
in `_gigachat.py` / `_yandex.py`. Functions here only know about OpenAI
Chat Completions shape (messages / choices / tool_calls) and JSON parsing.
"""
import json
import re
import uuid
from typing import Any

_MARKDOWN_FENCE_RE = re.compile(
    r"^```(?:json|JSON)?\s*\n(.*?)\n```\s*$", re.DOTALL,
)


def strip_markdown_fence(text: str) -> str:
    """Strip ``` ```json ... ``` ``` fence around a JSON object, if present."""
    s = text.strip()
    m = _MARKDOWN_FENCE_RE.match(s)
    if m:
        return m.group(1).strip()
    return s


def parse_json_object(text: str) -> dict | None:
    """Parse ``text`` as a JSON object. Return dict or None."""
    s = strip_markdown_fence(text)
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def detect_tool_call_from_payload(
    payload: dict, tools: list[dict]
) -> tuple[str, str] | None:
    """Match ``payload`` keys against declared ``tools`` to identify which
    tool the model intended to call.

    Two shapes accepted:
      1. Native OpenAI tool_call shape: ``{"name": "<tool>", "arguments": {...}}``.
         Use directly if name matches a declared tool.
      2. Flat argument payload: ``{"path": "/tmp/x"}``. Match by required+overlap.

    Returns ``(tool_name, arguments_json_str)`` or None.
    """
    if (
        isinstance(payload.get("name"), str)
        and "arguments" in payload
    ):
        name = payload["name"]
        args = payload["arguments"]
        for tool in tools:
            fn = tool.get("function") or {}
            if fn.get("name") == name:
                if isinstance(args, dict):
                    args_str = json.dumps(args, ensure_ascii=False)
                elif isinstance(args, str):
                    args_str = args
                else:
                    args_str = json.dumps(args, ensure_ascii=False)
                return name, args_str

    payload_keys = set(payload.keys())
    best_name: str | None = None
    best_score = -1
    for tool in tools:
        fn = tool.get("function") or {}
        name = fn.get("name")
        if not isinstance(name, str):
            continue
        params = fn.get("parameters") or {}
        required = set(params.get("required") or [])
        properties = set((params.get("properties") or {}).keys())
        if required and not required.issubset(payload_keys):
            continue
        score = len(payload_keys & properties)
        if score > best_score:
            best_score = score
            best_name = name
    if best_name is None or best_score == 0:
        return None
    args_json = json.dumps(payload, ensure_ascii=False)
    return best_name, args_json


def make_tool_call(name: str, arguments: Any, prefix: str) -> dict:
    """Build an OpenAI-shape tool_call envelope.

    ``arguments`` may be a dict (will be JSON-stringified) or a string
    (passed through).
    """
    if isinstance(arguments, dict):
        args_str = json.dumps(arguments, ensure_ascii=False)
    elif isinstance(arguments, str):
        args_str = arguments
    else:
        args_str = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": f"call_{prefix}_{uuid.uuid4().hex[:16]}",
        "type": "function",
        "function": {"name": name, "arguments": args_str},
    }


def set_message_tool_call(message: Any, tool_call: dict) -> bool:
    """Mutate ``message`` to carry a single ``tool_calls`` entry and clear content.

    Tolerant to both pydantic-model and dict representations of message.
    Returns True on success, False on failure (in which case caller skips).
    """
    if isinstance(message, dict):
        message["tool_calls"] = [tool_call]
        message["content"] = None
        return True
    try:
        message.tool_calls = [tool_call]
        message.content = None
        return True
    except (AttributeError, TypeError):
        try:
            message["tool_calls"] = [tool_call]
            message["content"] = None
            return True
        except Exception:
            return False


def set_choice_finish_reason(choice: Any, reason: str) -> None:
    """Set ``finish_reason`` on choice (pydantic-or-dict)."""
    for target in (choice, getattr(choice, "_choice", None)):
        if target is None:
            continue
        try:
            if isinstance(target, dict):
                target["finish_reason"] = reason
            else:
                target.finish_reason = reason
            return
        except (AttributeError, TypeError):
            continue


def iter_response_choices(response: Any) -> list:
    """Yield response choices regardless of pydantic-vs-dict shape."""
    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    return choices or []


def get_message_from_choice(choice: Any) -> Any:
    """Extract ``message`` from a choice in either pydantic or dict shape."""
    message = getattr(choice, "message", None)
    if message is None and isinstance(choice, dict):
        message = choice.get("message")
    return message


def get_message_field(message: Any, field: str) -> Any:
    """Read ``field`` from a message regardless of pydantic-vs-dict shape."""
    if isinstance(message, dict):
        return message.get(field)
    return getattr(message, field, None)
