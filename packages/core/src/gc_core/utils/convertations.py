from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Callable, Dict, List, Optional

from gc_core.utils.backend_errors import (
    nonempty_reasoning_text,
    normalize_finish_reason,
)

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


# Только закрытая пара, нежадно. Незакрытый ``<think>`` в прозе не трогаем
# (иначе «Use the <think> tag carefully.» → «Use the»).
_THINK_CLOSED_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _visible_prose_without_think(text: str) -> str:
    """Проза после вырезания закрытых ``<think>…</think>``. Без импорта projection."""
    if not isinstance(text, str) or not text:
        return ""
    return _THINK_CLOSED_RE.sub("", text).strip()


def _canon_content_text(content, *, persist_reasoning: bool):
    """В канон: think выбрасывается, если есть проза и конфиг его не хранит.

    Salvage ``<think>`` — только reasoning-only (нет прозы и нет тулов).
    ``persist_reasoning`` (YAML ``strip_think: false``) — content as-is.
    """
    if persist_reasoning or not isinstance(content, str):
        return content
    prose = _visible_prose_without_think(content)
    if prose and prose != content.strip():
        return prose
    return content


def _nonempty_content(content):
    """Строковый content после strip; пробельный → None. Не-строки as-is."""
    if content is None:
        return None
    if isinstance(content, str):
        return content.strip() or None
    return content


def _canonical_message_item(dct_choice, *, text, role, reasoning=None):
    item = {
        "id": "msg_" + get_md5_hash(dct_choice),
        "type": "message",
        "status": "completed",
        "content": [{
            "type": "output_text",
            "annotations": [],
            "logprobs": [],
            "text": text,
        }],
        "role": role,
    }
    if reasoning is not None:
        item["reasoning_content"] = reasoning
    return item


def get_output_list(dct, logger_override=None, *, persist_reasoning_content=False):
    """Transforms chat completion 'choices' into a standardized response 'output' list.

    ``reasoning_content`` копируется в канон только при ``persist_reasoning_content``.
    Reasoning-only без tools и без прозы (дефолтный strip) salvage в ``<think>…</think>``.
    Смешанный ход (think + проза): при strip think в канон не кладётся, остаётся проза.
    Совсем пустой choice — пустой output (цикл bounce), не ValueError.
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
        # Пробельный content/reasoning схлопываем до пустоты; дальше — те же ветки.
        content = _nonempty_content(msg_obj.get("content"))
        reasoning_text = nonempty_reasoning_text(msg_obj)
        has_tools = bool(msg_obj.get("tool_calls"))
        # Три флага независимы: не схлопывать в has_payload.
        # Иначе unknown finish + reasoning-only снова станет empty batch,
        # если has_payload сузят до content/tools.
        _decision = normalize_finish_reason(
            fr,
            has_content=bool(content),
            has_tool_calls=has_tools,
            has_reasoning=reasoning_text is not None,
        )
        if not _decision.accept:
            # Нет полезной нагрузки и неизвестный finish — не транспортный
            # ValueError: пустой батч, цикл сам bounce. Choice error уже выше.
            if logger_override is not None:
                logger_override.error(
                    "Empty choice discarded (unknown/empty finish_reason=%r): payload=%s",
                    fr, _safe_json_for_log(dct),
                )
            continue
        if _decision.unknown and logger_override is not None:
            logger_override.warning(
                "Unknown finish_reason=%r accepted permissively "
                "(content/tool_calls/reasoning present): payload=%s",
                fr, _safe_json_for_log(dct),
            )
        if fr == "length" and logger_override is not None:
            logger_override.warning(
                "chat/completions choice finished with length (output may be truncated; "
                "tool JSON may be incomplete). payload=%s",
                _safe_json_for_log(dct),
            )

        persist_field = reasoning_text if persist_reasoning_content else None

        text_out = None
        item_reasoning = persist_field if persist_reasoning_content else None
        if has_tools:
            # Случай A: tools. Salvage не трогает. Носитель CoT — только persist.
            if content is not None or item_reasoning is not None:
                raw = "" if content is None else content
                text_out = _canon_content_text(
                    raw, persist_reasoning=persist_reasoning_content
                )
        elif content:
            # Think + проза при strip — только проза; think-only content — as-is.
            text_out = _canon_content_text(
                content, persist_reasoning=persist_reasoning_content
            )
        elif reasoning_text is not None:
            if persist_reasoning_content:
                text_out = "" if content is None else content
            else:
                # Литеральный </think> внутри мысли иначе закрывает обёртку
                # раньше времени: нежадный strip оставляет хвост как прозу.
                body = re.sub(r"</think>", "", reasoning_text, flags=re.IGNORECASE)
                text_out = f"<think>{body}</think>"
                item_reasoning = None

        if text_out is not None:
            output_list.append(
                _canonical_message_item(
                    dct_choice,
                    text=text_out,
                    role=msg_obj.get("role"),
                    reasoning=item_reasoning,
                )
            )

        tool_calls = msg_obj.get("tool_calls")
        if tool_calls is not None:
            for tool_call_dict in tool_calls:
                if tool_call_dict['type'] != 'function':
                    raise ValueError("Unknown logic")

                id_tool_call = get_md5_hash(tool_call_dict)

                output_list.append({
                    "arguments": tool_call_dict['function']['arguments'],
                    "call_id": tool_call_dict['id'],
                    "name": tool_call_dict['function']['name'],
                    "type": "function_call",
                    "id": "fc_" + id_tool_call,
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
