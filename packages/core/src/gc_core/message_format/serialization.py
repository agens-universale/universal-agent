"""Каноничное текстовое представление сообщений треда для LLM-промптов и метрик длины."""

import re
from typing import Any, Dict, List, Sequence

from gc_core.attachments.model import (
    CONTEXT_CHARS_PER_IMAGE,
    CONTEXT_CHARS_PER_NOTE,
    ORIGIN_USER,
    attachments_of,
)
from gc_core.message_policy.user_message_policy import (
    is_dialog_summary_user_message_text,
    user_content_to_text,
)
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


# Контент любого сообщения (arguments/output/content) сводится к тексту по тем же правилам, что и
# user-контент: строка как есть, у элементов списка — поле ``text`` либо канонический JSON.
# Единственная реализация живёт в ``user_message_policy``; здесь — локальное имя для читаемости.
_message_content_to_text = user_content_to_text

_REASONING_LIMIT_CHARS = 8000
_REASONING_TAIL_FLOOR_CHARS = 1500
_REASONING_HEADER = "\n[reasoning]\n"


def _reasoning_source(message: Dict[str, Any]) -> str:
    """Сырой ``reasoning_content`` или алиас ``reasoning``, как в запросе к модели."""
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _clip_reasoning(text: str) -> str:
    """Начало и хвост в лимите 8000. Маркер — если середина не влезла."""
    if not text:
        return ""
    limit = _REASONING_LIMIT_CHARS
    half = limit // 2
    tail = max(_REASONING_TAIL_FLOOR_CHARS, half)
    head = max(0, limit - tail)
    cut = len(text) - head - tail
    if cut > 0:
        return f"{text[:head]}...[cutted {cut} chars...]...{text[-tail:]}"
    return text


def message_text_for_summary(
    message: Dict[str, Any],
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
    include_reasoning: bool = True,
) -> str:
    """
    Строит каноничное текстовое представление одного сообщения для LLM-промптов и метрик размера.

    Используется ``message_format.dialog_history``, суммаризацией и finish gate (trace digest).
    Для assistant при ``include_reasoning`` добавляет блок ``[reasoning]``: начало и хвост
    ``reasoning_content`` / ``reasoning`` в лимите 8000.
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
        label = (
            "function_call_output"
            if msg_type == "function_call_output"
            else "tool_call_output"
        )
        output = message.get("output")
        return f"[{label}]\n{_message_content_to_text(output)}"
    role = str(message.get("role") or "")
    raw_content = message.get("content")
    if role.strip().lower() == "assistant":
        content = project_assistant_text(
            raw_content, tags=tags, strip_think=strip_think
        )
        text = f"[{role}] {content}".strip()
        raw = _reasoning_source(message).strip()
        if raw and include_reasoning:
            text = f"{text}{_REASONING_HEADER}{_clip_reasoning(raw)}"
        return text
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
    payload, он уходит в основной запрос. ``reasoning_content`` /
    ``reasoning`` считается целиком — той длиной, которая уходит в запрос,
    а не урезанным блоком ``[reasoning]`` в промпте суммаризатора.

    Ваша картинка весит ``CONTEXT_CHARS_PER_IMAGE``: в ``content`` её нет, а
    контекст модели она занимает (спека §5); снимок агента — записку
    (``CONTEXT_CHARS_PER_NOTE``): пикселями он уходит только в своём ходе. Не
    покрыто: ваши картинки старше окна живости считаются картинками, снимки
    текущего хода — записками (реальный объём ловит токенный порог). Этим же счётом живут агрегаты
    ``persistence.live_volume``, индикатор контекста TUI в символьной шкале и хвост
    токенной оценки порога (после последнего запроса, до числа от бэкенда).
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
        projected = project_assistant_text(
            raw_content, tags=tags, strip_think=strip_think
        )
        prose = (
            len(projected)
            if projected
            else len(_message_content_to_text(raw_content))
        )
        return prose + len(_reasoning_source(message))
    return len(_message_content_to_text(raw_content)) + sum(
        CONTEXT_CHARS_PER_IMAGE
        if att.origin == ORIGIN_USER
        else CONTEXT_CHARS_PER_NOTE
        for att in attachments_of(message)
    )


def calc_history_chars_for_trigger(
    messages: List[Dict[str, Any]],
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> int:
    """Суммарный объём истории в «сырых» символах для ``chat_completions_summary_trigger_chars``."""
    return sum(
        message_raw_payload_chars_for_trigger(
            m, tags=tags, strip_think=strip_think
        )
        for m in messages
    )


def calc_messages_chars_total(
    messages: List[Dict[str, Any]],
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> int:
    """Суммарная длина сериализованного треда (каноничный вид для промпта LLM)."""
    total = 0
    for message in messages:
        total += len(
            message_text_for_summary(
                message, tags=tags, strip_think=strip_think
            )
        )
    return total
