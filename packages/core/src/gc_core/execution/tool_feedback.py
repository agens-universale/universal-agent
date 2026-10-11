import difflib
import json
from typing import Any, Dict, Iterable

_LEGACY_UNKNOWN_TOOL_MARKER = "there is not such tool"

INTERNAL_TOOL_ERROR = "internal_tool_error"
_INTERNAL_TOOL_ERROR_MESSAGE_CAP = 500
_INTERNAL_TOOL_ERROR_GUIDANCE_CAP = 800
_INTERNAL_TOOL_ERROR_GUIDANCE = (
    "Client runtime failed while executing the tool "
    "(not an arguments-schema rejection). "
    "Do not retry the identical call hoping for a different schema outcome. "
    "Change strategy, pick another tool, or report the failure if the task is blocked."
)


def _truncate_for_tool_output(text: str, cap: int) -> str:
    if cap <= 0:
        return ""
    if len(text) <= cap:
        return text
    if cap <= 3:
        return text[:cap]
    return text[: cap - 3] + "..."


def format_internal_tool_error_output(
    *,
    tool_name: str | None,
    exc: BaseException,
    call_id: str | None = None,
) -> str:
    """Slim JSON tool output for unexpected exceptions in apply_tool (no traceback)."""
    message = str(exc).strip()
    if not message:
        message = type(exc).__name__
    message = _truncate_for_tool_output(
        message.replace("\n", " ").strip(),
        _INTERNAL_TOOL_ERROR_MESSAGE_CAP,
    )
    guidance = _truncate_for_tool_output(
        _INTERNAL_TOOL_ERROR_GUIDANCE,
        _INTERNAL_TOOL_ERROR_GUIDANCE_CAP,
    )
    payload: Dict[str, Any] = {
        "ok": False,
        "error": INTERNAL_TOOL_ERROR,
        "exception_type": type(exc).__name__,
        "message": message,
        "guidance": guidance,
    }
    if isinstance(tool_name, str) and tool_name:
        payload["tool"] = tool_name
    if isinstance(call_id, str) and call_id:
        payload["call_id"] = call_id
    return json.dumps(payload, ensure_ascii=False)


def is_internal_tool_error_output(output: str) -> bool:
    if not isinstance(output, str) or not output:
        return False
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    return (
        parsed.get("ok") is False
        and parsed.get("error") == INTERNAL_TOOL_ERROR
    )


def format_unknown_tool_output(
    tool_name: str, available_names: Iterable[str]
) -> str:
    suggestions = difflib.get_close_matches(
        tool_name, list(available_names), n=5, cutoff=0.3
    )
    error_message = f"No tool named '{tool_name}' on client."
    use_only = "Use only tools from the provided tool list in section <tools> in system prompt."
    if suggestions:
        message = f"Unknown tool '{tool_name}'. Maybe you mean tools: {', '.join(suggestions)}. {use_only}"
    else:
        message = f"Unknown tool '{tool_name}'. {use_only}"
    payload = {
        "ok": False,
        "error": {
            "type": "unknown_tool",
            "message": error_message,
            "requested": tool_name,
            "suggestions": suggestions,
        },
        "message": message,
    }
    return json.dumps(payload, ensure_ascii=False)


def is_unknown_tool_output(output: str) -> bool:
    if not isinstance(output, str) or not output:
        return False
    if _LEGACY_UNKNOWN_TOOL_MARKER in output:
        return True
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    error = parsed.get("error")
    return isinstance(error, dict) and error.get("type") == "unknown_tool"
