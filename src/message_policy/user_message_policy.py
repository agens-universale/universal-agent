import json
import re
from typing import Any, Dict, List

TECHNICAL_TYPE_TECHNICAL = "technical"
TECHNICAL_TYPE_ERROR_MESSAGE = "error_message"
TECHNICAL_TYPE_DIALOG_SUMMARY = "dialog_summary"
TECHNICAL_TYPE_CURRENT_CONTEXT = "current_context"

_TECHNICAL_CONTENT_RE = re.compile(
    r"<technical\s+type=\"([a-z_]+)\">(.*?)</technical>",
    re.IGNORECASE | re.DOTALL,
)
_TECHNICAL_TYPES = {
    TECHNICAL_TYPE_TECHNICAL,
    TECHNICAL_TYPE_ERROR_MESSAGE,
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    TECHNICAL_TYPE_CURRENT_CONTEXT,
}

_TECHNICAL_USER_MARKERS = (
    "<current_context>",
    "<technical>",
    "<technical ",
    "<error_message>",
    "[user][technical]",
    "client has non-empty thread notes;",
    "client has non-empty memory files;",
    "there is no tool call in agent response",
    "tool argument validation failed",
    "expected schema:",
    "received arguments:",
    "<dialog_summary>",
)


def is_technical_user_message_text(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in _TECHNICAL_USER_MARKERS)


def wrap_technical_user_content(
    content: str, *, technical_type: str = TECHNICAL_TYPE_TECHNICAL
) -> str:
    raw = str(content or "").strip()
    if technical_type not in _TECHNICAL_TYPES:
        raise ValueError(f"Unsupported technical_type: {technical_type!r}")
    if not raw:
        return f'<technical type="{technical_type}"></technical>'
    match = _TECHNICAL_CONTENT_RE.fullmatch(raw)
    if match:
        return raw
    return f'<technical type="{technical_type}">{raw}</technical>'


def wrap_skill_resolution_technical_instruction(skill_name: str) -> str:
    """
    Technical-сообщение для CLI: решать запрос через skill с данным именем;
    при отсутствии — похожее имя (опечатка), если описание подходит; иначе fail.
    """
    safe = (
        str(skill_name)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    body = (
        f'For resolving user ask use skill "{safe}". '
        "If such skill is not available, use a skill with a similar name "
        "(user may have mistyped the skill name) if such a skill matches the "
        "user task according to its skill description. "
        "If no suitable skill is available, report failure and stop."
    )
    return wrap_technical_user_content(body)


def is_dialog_summary_user_message_text(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    if '<technical type="dialog_summary">' in normalized:
        return True
    return (
        "summarized context from previous dialogue." in normalized
        or "<dialog_summary>" in normalized
    )


def user_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
            parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    if content is None:
        return ""
    return str(content)


def extract_real_user_message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    if str(message.get("role", "")).strip().lower() != "user":
        return ""
    content = user_content_to_text(message.get("content")).strip()
    if not content:
        return ""
    if is_technical_user_message_text(content):
        return ""
    return content


def resolve_technical_user_content_type(content: str) -> str:
    normalized = str(content or "").strip().lower()
    if (
        "tool argument validation failed" in normalized
        or "expected schema:" in normalized
        or "received arguments:" in normalized
    ):
        return TECHNICAL_TYPE_ERROR_MESSAGE
    if (
        "<current_context>" in normalized
        or "client has non-empty thread notes;" in normalized
        or "client has non-empty memory files;" in normalized
    ):
        return TECHNICAL_TYPE_CURRENT_CONTEXT
    if is_dialog_summary_user_message_text(content):
        return TECHNICAL_TYPE_DIALOG_SUMMARY
    return TECHNICAL_TYPE_TECHNICAL


def normalize_user_log_entries(entries: Any) -> List[str]:
    out: List[str] = []
    if not isinstance(entries, list):
        return out
    for item in entries:
        text = str(item).strip()
        if not text:
            continue
        if is_technical_user_message_text(text):
            continue
        if text not in out:
            out.append(text)
    return out


def append_user_log_entries(existing: List[str], step_messages: List[Dict[str, Any]]) -> List[str]:
    out = list(existing) if isinstance(existing, list) else []
    seen = set(out)
    for message in step_messages:
        entry = extract_real_user_message_text(message)
        if not entry:
            continue
        if entry not in seen:
            out.append(entry)
            seen.add(entry)
    return out


def build_invalid_tool_call_feedback_message(message: dict) -> dict:
    tool_name = str(message.get("name", "unknown_tool"))
    raw_args = message.get("arguments")
    args_text = str(raw_args) if raw_args is not None else "(missing)"
    try:
        json.loads(args_text)
        error_text = "tool arguments were marked invalid by pre-send validation"
    except Exception as exc:  # noqa: BLE001
        error_text = f"invalid JSON in tool arguments: {exc}"

    mitigation = ""
    if tool_name in ("write_to_file", "append_to_file"):
        mitigation = (
            "\n\nRegenerate with valid JSON. For large file bodies, follow the tool "
            "descriptions: brief `write_to_file` then multiple `append_to_file` calls "
            "with moderate chunks-not one oversized `arguments` payload."
        )

    feedback = (
        "Tool call cannot be sent to chat/completions backend because arguments are invalid.\n\n"
        f"Tool name: {tool_name}\n"
        f"Validation error: {error_text}\n\n"
        "Received tool arguments:\n"
        "```json\n"
        f"{args_text}\n"
        "```\n\n"
        "Please regenerate this tool call with valid JSON arguments."
        f"{mitigation}"
    )
    return {
        "role": "user",
        "content": wrap_technical_user_content(
            feedback, technical_type=TECHNICAL_TYPE_ERROR_MESSAGE
        ),
    }
