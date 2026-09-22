"""Каноничное текстовое представление сообщений треда для LLM-промптов и метрик длины."""

import json
import re
from typing import Any, Dict, List, Sequence

from gc_core.message_policy.user_message_policy import is_dialog_summary_user_message_text, user_content_to_text
from gc_core.message_projection import project_assistant_text

_ORIGINAL_USER_MESSAGES_BLOCK_RE = re.compile(
    r"<original_user_messages>.*?</original_user_messages>\s*",  # NOSONAR: внутри блока бывают '<'
    re.IGNORECASE | re.DOTALL,
)


def strip_original_user_messages_block(summary_text: str) -> str:
    """
    Удаляет из текста фрагмент ``<original_user_messages>...</original_user_messages>``.

    Зачем: при **входе в LLM-промпт** этот блок дублирует то, что уже передаётся отдельным полем
    ``<user_requests_log>`` в user-промпте.

    Аргументы:
        summary_text: исходная строка саммари. Не-``str`` трактуется как отсутствие данных.

    Возвращает:
        Очищенную строку либо ``""``, если вход не строка или после очистки текст пуст.
    """
    if not isinstance(summary_text, str):
        return ""
    cleaned = _ORIGINAL_USER_MESSAGES_BLOCK_RE.sub("", summary_text)
    return cleaned.strip()


def history_has_dialog_summary_message(messages: List[Dict[str, Any]]) -> bool:
    """
    Проверяет, есть ли в истории уже вставленное техническое user-сообщение с типом dialog summary.

    Зачем: оркестратор (например, ``agent_executor``) различает «сырой» тред и тред после compaction —
    без этого легко повторить суммаризацию или неверно обработать загруженный из persistence трейс.
    """
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).strip().lower() != "user":
            continue
        content = user_content_to_text(msg.get("content")).strip()
        if not content:
            continue
        if is_dialog_summary_user_message_text(content):
            return True
    return False


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
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


def message_text_for_summary(
    message: Dict[str, Any],
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> str:
    """
    Строит каноничное текстовое представление одного сообщения для LLM-промптов и метрик размера.

    Используется ``message_format.dialog_history``, суммаризацией и finish gate (trace digest).
    """
    if not isinstance(message, dict):
        return ""
    msg_type = str(message.get("type") or "message")
    if msg_type in ("function_call", "tool_call"):
        label = "function_call" if msg_type == "function_call" else "tool_call"
        name = str(message.get("name") or "")
        arguments = message.get("arguments")
        return f"[{label}] {name}\n{_message_content_to_text(arguments)}"
    if msg_type in ("function_call_output", "tool_call_output"):
        label = "function_call_output" if msg_type == "function_call_output" else "tool_call_output"
        output = message.get("output")
        return f"[{label}]\n{_message_content_to_text(output)}"
    role = str(message.get("role") or "")
    raw_content = message.get("content")
    if role.strip().lower() == "assistant":
        content = project_assistant_text(raw_content, tags=tags, strip_think=strip_think)
    else:
        content = _message_content_to_text(raw_content)
    return f"[{role}] {content}".strip()


def message_raw_payload_chars_for_trigger(
    message: Dict[str, Any],
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> int:
    """Оценка длины содержимого для порога суммаризации, без префиксов ``[role]``.

    Assistant: видимая проза при ``strip_think``; think-only (salvage) — сырой
    payload, он уходит в основной запрос.
    """
    if not isinstance(message, dict):
        return 0
    msg_type = str(message.get("type") or "message")
    if msg_type in ("function_call", "tool_call"):
        name = str(message.get("name") or "")
        arg_text = _message_content_to_text(message.get("arguments"))
        return len(name) + len(arg_text)
    if msg_type in ("function_call_output", "tool_call_output"):
        return len(_message_content_to_text(message.get("output")))
    role = str(message.get("role") or "")
    raw_content = message.get("content")
    if role.strip().lower() == "assistant":
        projected = project_assistant_text(raw_content, tags=tags, strip_think=strip_think)
        if projected:
            return len(projected)
        return len(_message_content_to_text(raw_content))
    return len(_message_content_to_text(raw_content))


def calc_history_chars_for_trigger(
    messages: List[Dict[str, Any]],
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> int:
    """Суммарный объём истории в «сырых» символах для ``chat_completions_summary_trigger_chars``."""
    return sum(message_raw_payload_chars_for_trigger(m, tags=tags, strip_think=strip_think) for m in messages)


def calc_messages_chars_total(
    messages: List[Dict[str, Any]],
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> int:
    """Суммарная длина сериализованного треда (каноничный вид для промпта LLM)."""
    total = 0
    for message in messages:
        total += len(message_text_for_summary(message, tags=tags, strip_think=strip_think))
    return total
