import json
from copy import deepcopy

cut_message = 350


def cut_message_f(s):
    s = str(s)
    return s if len(str(s)) < cut_message else str(s)[:cut_message] + "\n...[cutted]"


def format_type_message(message):
    role = message.get("role", "Unknown role")
    content = message.get("content", [])

    if "role" == "system":
        return ""

    tmpl = ""

    if isinstance(content, str):
        if len(content) == 0:
            return ""

        tmpl = f"\n\n>>>>>>>>>>\n{role}: {content}\n"
        return tmpl

    parts = []
    if isinstance(content, list):
        for m in content:
            text = m.get("text", "")
            if len(text) > 0:
                parts.append(text)
        parts_text = "\n\n".join(parts)

        if len(parts_text) > 0:
            tmpl = f"\n\n>>>>>>>>>>\n{role}: {parts_text}\n"
            return tmpl
        else:
            return ""
    else:
        return json.dumps(message, ensure_ascii=False, indent=2)


def format_type_functions_call(message):
    tmpl = f"\n\n>>>>>>>>>>\ncall_id: {message['call_id']}:\n{message['name']}\n"
    try:
        args = []
        for k, v in json.loads(message["arguments"]).items():
            v_ = cut_message_f(v)
            args.append(f"{k}: {v_}")
        tmpl += "\n".join(args)
    except Exception as e:
        tmpl += str(e) + "\n"
        tmpl += cut_message_f(message)
    return tmpl


def format_type_function_call_output(message):
    if "output" in message:
        text = message["output"]
    else:
        text = message["content"]
    text = str(text)
    output = cut_message_f(text)
    tmpl = f"\n\n>>>>>>>>>>\ncompleted: {message['call_id']}:\n{output}\n"
    return tmpl


def format_others_responses(message):
    if message.get("role") == "system":
        return "\n\n>>>>>>>>>>\nsystem: <local system prompt. See debug logs>"
    if message.get("role") == "user":
        return f"\n\n>>>>>>>>>>\nuser: {message['content']}"
    tmpl = "\n\n>>>>>>>>>>\n" + json.dumps(message, ensure_ascii=False, indent=2)
    return tmpl


def format_message_responses(message):
    m_type = message.get("type", None)
    if m_type == "message":
        return format_type_message(message)
    elif m_type == "function_call_output":
        return format_type_function_call_output(message)
    elif m_type == "function_call":
        return format_type_functions_call(message)
    else:
        return format_others_responses(message)


def _extract_text_from_canonical_message_content(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n\n".join(parts)


def _canonical_to_chat_completions_message(message):
    """Канон ``state.messages`` → chat/completions item. Проза as-is, без проекции."""
    m_type = message.get("type")
    if m_type == "message":
        role = message.get("role", "assistant")
        converted = {
            "role": role,
            "content": _extract_text_from_canonical_message_content(message.get("content")),
        }
        if "reasoning_content" in message:
            converted["reasoning_content"] = message["reasoning_content"]
        return converted
    if m_type == "function_call":
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": message["call_id"],
                    "type": "function",
                    "function": {
                        "name": message["name"],
                        "arguments": message["arguments"],
                    },
                }
            ],
        }
    if m_type == "function_call_output":
        output = message.get("output")
        if output is None:
            output = message.get("content", "")
        return {
            "role": "tool",
            "tool_call_id": message["call_id"],
            "content": str(output),
        }
    return None


def _legacy_role_to_chat_completions_message(message):
    m_role = message.get("role")
    if m_role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message["call_id"],
            "content": message.get("content", message.get("output", "")),
        }

    if m_role == "system":
        return {"role": "system", "content": message["content"]}

    if m_role == "user":
        return {"role": "user", "content": message["content"]}

    if m_role == "assistant":
        converted = {
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": deepcopy(message.get("tool_calls", list())),
        }
        if "reasoning_content" in message:
            converted["reasoning_content"] = message["reasoning_content"]
        return converted
    return None


def format_message_chat_completions_to_send_backend(message):
    converted = _canonical_to_chat_completions_message(message)
    if converted is not None:
        return converted

    converted = _legacy_role_to_chat_completions_message(message)
    if converted is not None:
        return converted

    return message
