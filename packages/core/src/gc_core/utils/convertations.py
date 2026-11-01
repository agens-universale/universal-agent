from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Callable, Dict, List, Optional

from gc_core.execution.finish_reason import (
    FINISH_KIND_LENGTH,
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
        if message.get("type") != "function_call" or _has_valid_json_arguments(
            message
        ):
            sanitized.append(message)
            continue
        if callable(invalid_tool_call_factory):
            replaced = invalid_tool_call_factory(message)
            if isinstance(replaced, dict):
                sanitized.append(replaced)
        # Без фабрики просто не отправляем невалидный function_call.
    return sanitized


def _has_valid_json_arguments(message: Dict) -> bool:
    raw_args = message.get("arguments")
    if not isinstance(raw_args, str) or not raw_args.strip():
        return False
    try:
        json.loads(raw_args)
    except Exception:
        return False
    return True


def get_md5_hash(obj):
    """Returns an md5 hash of a Python object for consistent IDs."""
    # For dicts/lists, use json serialization for stable hashing
    s = json.dumps(obj)
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# Только закрытая пара, нежадно. Незакрытый ``<think>`` в прозе не трогаем
# (иначе «Use the <think> tag carefully.» → «Use the»).
_THINK_CLOSED_RE = re.compile(
    r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE
)


def closed_think_texts(text: str) -> list[str]:
    """Тексты закрытых ``<think>…</think>`` по порядку, без пустых."""
    if not isinstance(text, str):
        return []
    pieces = (p.strip() for p in _THINK_CLOSED_RE.findall(text))
    return [p for p in pieces if p]


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
    """Preserve nonempty content verbatim, including whitespace-only strings."""
    if content is None:
        return None
    if isinstance(content, str):
        return content or None
    return content


def _canonical_message_item(
    dct_choice, *, text, role, reasoning=None, response_id=None
):
    item = {
        "id": "msg_"
        + get_md5_hash({"response_id": response_id, "choice": dct_choice}),
        "type": "message",
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "annotations": [],
                "logprobs": [],
                "text": text,
            }
        ],
        "role": role,
    }
    if reasoning is not None:
        item["reasoning_content"] = reasoning
    return item


def get_output_list(
    dct, logger_override=None, *, persist_reasoning_content=False
):
    """Transforms chat completion 'choices' into a standardized response 'output' list.

    ``reasoning_content`` копируется в канон только при ``persist_reasoning_content``.
    Reasoning-only без tools и без прозы (дефолтный strip) salvage в ``<think>…</think>``.
    Смешанный ход (think + проза): при strip think в канон не кладётся, остаётся проза.
    Tools + think-only content при strip: message не эмитим (только function_call).
    Совсем пустой choice — пустой output (цикл bounce), не ValueError.
    """
    output_list = []
    for dct_choice in dct["choices"]:
        choice_error = dct_choice.get("error")
        if isinstance(choice_error, dict):
            err_code = choice_error.get("code")
            err_message = choice_error.get("message")
            err_metadata = choice_error.get("metadata")
            raise ValueError(
                "Chat completion provider returned choice error: "
                f"code={err_code!r} message={err_message!r} metadata={err_metadata!r}"
            )
        msg_obj = dct_choice.get("message") or {}
        # Keep nonempty provider strings verbatim, including whitespace-only text.
        content = _nonempty_content(msg_obj.get("content"))
        reasoning_text = nonempty_reasoning_text(msg_obj)
        has_tools = bool(msg_obj.get("tool_calls"))
        if not _accept_choice_finish_reason(
            dct,
            dct_choice.get("finish_reason"),
            logger_override,
            has_content=bool(content),
            has_tool_calls=has_tools,
            has_reasoning=reasoning_text is not None,
        ):
            continue

        text_out, item_reasoning = _message_text_and_reasoning(
            content,
            reasoning_text,
            has_tools=has_tools,
            persist_reasoning_content=persist_reasoning_content,
        )
        if text_out is not None:
            output_list.append(
                _canonical_message_item(
                    dct_choice,
                    text=text_out,
                    role=msg_obj.get("role"),
                    reasoning=item_reasoning,
                    response_id=dct.get("id"),
                )
            )

        tool_calls = msg_obj.get("tool_calls")
        if tool_calls is not None:
            for tool_call_dict in tool_calls:
                output_list.append(_function_call_item(tool_call_dict))

    return output_list


def _accept_choice_finish_reason(
    dct,
    fr,
    logger_override,
    *,
    has_content: bool,
    has_tool_calls: bool,
    has_reasoning: bool,
) -> bool:
    """Решение по finish_reason choice; спорные finish_reason — в лог.

    False — choice без полезной нагрузки: его отбрасываем (пустой батч).
    """
    # Три флага независимы: не схлопывать в has_payload.
    # Иначе unknown finish + reasoning-only снова станет empty batch,
    # если has_payload сузят до content/tools.
    _decision = normalize_finish_reason(
        fr,
        has_content=has_content,
        has_tool_calls=has_tool_calls,
        has_reasoning=has_reasoning,
    )
    if not _decision.accept:
        # Нет полезной нагрузки и неизвестный finish — не транспортный
        # ValueError: пустой батч, цикл сам bounce. Choice error уже выше.
        if logger_override is not None:
            logger_override.error(
                "Empty choice discarded (unknown/empty finish_reason=%r): payload=%s",
                fr,
                _safe_json_for_log(dct),
            )
        return False
    if logger_override is None:
        return True
    if _decision.unknown:
        logger_override.warning(
            "Unknown finish_reason=%r accepted permissively (content/tool_calls/reasoning present): payload=%s",
            fr,
            _safe_json_for_log(dct),
        )
    if _decision.kind == FINISH_KIND_LENGTH:
        logger_override.warning(
            "chat/completions choice finished with length (output may be truncated; "
            "tool JSON may be incomplete). payload=%s",
            _safe_json_for_log(dct),
        )
    return True


def _message_text_and_reasoning(
    content,
    reasoning_text,
    *,
    has_tools: bool,
    persist_reasoning_content: bool,
):
    """→ (text message-item канона либо None — message не эмитим, его reasoning_content)."""
    item_reasoning = reasoning_text if persist_reasoning_content else None
    if has_tools:
        # Случай A: tools. Salvage не трогает. CoT в историю — только persist.
        # Think-only inline content при strip не эмитим (как reasoning_content).
        text_out = _message_text_with_tools(
            content,
            item_reasoning=item_reasoning,
            persist_reasoning_content=persist_reasoning_content,
        )
        return text_out, item_reasoning
    if content and (
        persist_reasoning_content
        or not isinstance(content, str)
        or content.strip()
        or reasoning_text is None
    ):
        # Think + проза при strip — только проза; think-only content — as-is.
        text_out = _canon_content_text(
            content, persist_reasoning=persist_reasoning_content
        )
        return text_out, item_reasoning
    if reasoning_text is None:
        return None, item_reasoning
    if persist_reasoning_content:
        return ("" if content is None else content), item_reasoning
    # Литеральный </think> внутри мысли иначе закрывает обёртку
    # раньше времени: нежадный strip оставляет хвост как прозу.
    body = re.sub(r"</think>", "", reasoning_text, flags=re.IGNORECASE)
    # In strip mode salvage reasoning before the original whitespace content.
    suffix = content if isinstance(content, str) else ""
    return f"<think>{body}</think>{suffix}", None


def _message_text_with_tools(
    content, *, item_reasoning, persist_reasoning_content: bool
):
    """Text message-item хода с tool_calls либо None (message не эмитим)."""
    if persist_reasoning_content:
        if content is None and item_reasoning is None:
            return None
        raw = "" if content is None else content
        return _canon_content_text(raw, persist_reasoning=True)
    if content is None:
        return None
    if not isinstance(content, str):
        return content
    if content and not content.strip():
        return content
    if not _visible_prose_without_think(content):
        return None
    return _canon_content_text(content, persist_reasoning=False)


def _function_call_item(tool_call_dict) -> dict:
    if tool_call_dict["type"] != "function":
        raise ValueError("Unknown logic")
    id_tool_call = get_md5_hash(tool_call_dict)
    return {
        "arguments": tool_call_dict["function"]["arguments"],
        "call_id": tool_call_dict["id"],
        "name": tool_call_dict["function"]["name"],
        "type": "function_call",
        "id": "fc_" + id_tool_call,
        "status": "completed",
    }


def output_chat_completion_to_response(
    dct,
    logger_override=None,
    log_context=None,
    *,
    persist_reasoning_content=False,
):
    """Converts a chat completion response dict to a model response format."""
    dct_output = deepcopy(dct)
    dct_output["id"] = dct_output.get("id", get_md5_hash(dct_output))
    # Backends may return either `created` (chat/completions-like)
    # or already normalized `created_at`.
    created = dct_output.pop("created", None)
    if created is not None:
        dct_output["created_at"] = created
    elif "created_at" not in dct_output:
        dct_output["created_at"] = None

    try:
        dct_output["output"] = get_output_list(
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
    if "system_fingerprint" in dct_output:
        del dct_output["system_fingerprint"]

    dct_output["parallel_tool_calls"] = True
    dct_output["status"] = "completed"
    dct_output["object"] = "response"
    return dct_output
