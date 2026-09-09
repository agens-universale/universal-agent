import json
import hashlib
from copy import deepcopy
from typing import Callable, Dict, List, Optional

from gc_core.utils.backend_errors import normalize_finish_reason

_MAX_BACKEND_LOG_CHARS = 8000


def _safe_json_for_log(payload, *, max_chars=_MAX_BACKEND_LOG_CHARS):
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        raw = str(payload)
    if len(raw) <= max_chars:
        return raw
    return f"{raw[:max_chars]}... [truncated, total_chars={len(raw)}]"


def sanitize_messages_for_chat_completions(
    messages: List[Dict],
    *,
    invalid_tool_call_factory: Optional[Callable[[Dict], Dict]] = None,
) -> List[Dict]:
    """
    Техническая нормализация истории перед chat/completions:
    function_call с невалидным JSON в arguments заменяется фабрикой,
    либо пропускается, если фабрика не передана.
    """
    sanitized: List[Dict] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("type") != "function_call":
            sanitized.append(message)
            continue

        raw_args = message.get("arguments")
        is_valid_json_str = isinstance(raw_args, str) and bool(raw_args.strip())
        if is_valid_json_str:
            try:
                json.loads(raw_args)
                sanitized.append(message)
                continue
            except Exception:  # noqa: BLE001
                pass

        if callable(invalid_tool_call_factory):
            replaced = invalid_tool_call_factory(message)
            if isinstance(replaced, dict):
                sanitized.append(replaced)
        # Без фабрики просто не отправляем невалидный function_call.
    return sanitized


def get_md5_hash(obj):
    """Returns an md5 hash of a Python object for consistent IDs."""
    # For dicts/lists, use json serialization for stable hashing
    s = json.dumps(obj)
    return hashlib.md5(s.encode('utf-8')).hexdigest()


def get_output_list(dct, logger_override=None, *, persist_reasoning_content=False):
    """Transforms chat completion 'choices' into a standardized response 'output' list.

    ``reasoning_content`` копируется в канон только при ``persist_reasoning_content``.
    """
    output_list = []
    for dct_choice in dct['choices']:
        choice_error = dct_choice.get("error")
        if isinstance(choice_error, dict):
            err_code = choice_error.get("code")
            err_message = choice_error.get("message")
            err_metadata = choice_error.get("metadata")
            raise ValueError(
                "Chat completion provider returned choice error: "
                f"code={err_code!r} message={err_message!r} metadata={err_metadata!r}"
            )
        fr = dct_choice.get("finish_reason")
        msg_obj = dct_choice.get("message") or {}
        _decision = normalize_finish_reason(
            fr,
            has_content=bool(msg_obj.get("content")),
            has_tool_calls=bool(msg_obj.get("tool_calls")),
        )
        if not _decision.accept:
            if logger_override is not None:
                logger_override.error(
                    "Empty/unusable chat/completions response (finish_reason=%r, "
                    "no content/tool_calls): payload=%s",
                    fr, _safe_json_for_log(dct),
                )
            raise ValueError(
                f"Backend returned empty/unusable response (finish_reason={fr!r})"
            )
        if _decision.unknown and logger_override is not None:
            logger_override.warning(
                "Unknown finish_reason=%r accepted permissively (content/tool_calls "
                "present): payload=%s",
                fr, _safe_json_for_log(dct),
            )
        if fr == "length" and logger_override is not None:
            logger_override.warning(
                "chat/completions choice finished with length (output may be truncated; "
                "tool JSON may be incomplete). payload=%s",
                _safe_json_for_log(dct),
            )

        # Message-item: есть проза, либо persist_reasoning_content и есть
        # reasoning_content при content=None — носитель CoT на tool-only такте.
        # Пустой message без ризонинга не плодим.
        content = msg_obj.get("content")
        reasoning = None
        if persist_reasoning_content:
            reasoning = msg_obj.get("reasoning_content")
        if content is not None or reasoning is not None:
            id_choice = get_md5_hash(dct_choice)
            item = {
                "id": "msg_" + id_choice,
                "type": "message",
                "status": "completed",
                "content": [{
                    "type": "output_text",
                    "annotations": [],
                    "logprobs": [],
                    "text": "" if content is None else content
                }],
                "role": msg_obj.get("role")
            }
            if reasoning is not None:
                item["reasoning_content"] = reasoning
            output_list.append(item)

        # --- FIX 2: Generate a unique ID for each tool call ---

        if dct_choice['message'].get("tool_calls", None) is not None:
            for tool_call_dict in dct_choice['message']['tool_calls']:
                if tool_call_dict['type'] != 'function':
                    raise ValueError("Unknown logic")

                # Generate a unique hash for THIS specific tool call
                id_tool_call = get_md5_hash(tool_call_dict)

                output_list.append({
                    "arguments": tool_call_dict['function']['arguments'],
                    "call_id": tool_call_dict['id'],
                    "name": tool_call_dict['function']['name'],
                    "type": "function_call",
                    "id": "fc_" + id_tool_call,  # Use the new unique ID
                    "status": "completed"
                })

    return output_list


def output_chat_completion_to_response(
    dct, logger_override=None, log_context=None, *, persist_reasoning_content=False
):
    """Converts a chat completion response dict to a model response format."""
    dct_output = deepcopy(dct)
    dct_output['id'] = dct_output.get('id', get_md5_hash(dct_output))
    # Backends may return either `created` (chat/completions-like)
    # or already normalized `created_at`.
    created = dct_output.pop("created", None)
    if created is not None:
        dct_output["created_at"] = created
    elif "created_at" not in dct_output:
        dct_output["created_at"] = None

    try:
        dct_output['output'] = get_output_list(
            dct_output,
            logger_override=logger_override,
            persist_reasoning_content=persist_reasoning_content,
        )
    except Exception:
        if logger_override is not None:
            logger_override.error(
                "Failed to convert chat/completions payload. context=%s payload=%s",
                _safe_json_for_log(log_context or {}),
                _safe_json_for_log(dct),
                exc_info=True,
            )
        raise
    if 'system_fingerprint' in dct_output:
        del dct_output['system_fingerprint']

    dct_output['parallel_tool_calls'] = True
    dct_output['status'] = "completed"
    dct_output['object'] = 'response'
    return dct_output
