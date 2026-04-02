import json
import re
from typing import Any, Dict, List

from user_message_policy import (
    is_dialog_summary_user_message_text,
    user_content_to_text,
)

_ORIGINAL_USER_MESSAGES_BLOCK_RE = re.compile(
    r"<original_user_messages>.*?</original_user_messages>\s*",
    re.IGNORECASE | re.DOTALL,
)


def strip_original_user_messages_block(summary_text: str) -> str:
    """
    Удаляет из текста фрагмент ``<original_user_messages>...</original_user_messages>``.

    Зачем: при **входе в LLM-суммаризацию** этот блок дублирует то, что уже передаётся отдельным полем
    ``<user_requests_log>`` в user-промпте 

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

    Что делает: проходит по списку и вызывает ``is_dialog_summary_message`` для каждого элемента.

    Аргументы:
        messages: последовательность сообщений (как в OpenAI-совместимом API). Не-словари пропускаются.

    Возвращает:
        ``True``, если найдено непустое user-сообщение, распознаваемое как dialog summary; иначе ``False``.
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
    """
    Приводит поле ``content`` (или payload tool-вызова) к одной строке для встраивания в текст саммари.

    Зачем: ``message_text_for_summary`` должен работать и с чистой строкой, и с multimodal-списком,
    и с произвольным JSON — без дублирования правил в нескольких ветках.

    Что делает: строка возвращается как есть; список — конкатенация текстовых частей и JSON остального;
    dict — ``json.dumps``; ``None`` — ``""``; прочие типы — ``str(...)``.

    Аргументы:
        content: произвольное значение из тела сообщения или аргументов/вывода function_call.

    Возвращает:
        Скалярную строку без побочных эффектов.
    """
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


def message_text_for_summary(message: Dict[str, Any]) -> str:
    """
    Строит каноничное текстовое представление одного сообщения для LLM-суммаризации и метрик размера.

    Зачем: ``dialog_history`` / оркестратор суммаризации и ``calc_messages_chars_total`` должны смотреть на один и тот же
    сериализованный вид, иначе пороги по символам и текст для модели разъедутся.

    Что делает: для ``function_call`` / ``tool_call`` и вывода инструмента формирует помеченные блоки;
    для обычных сообщений — ``[<role>]`` плюс текст из ``_message_content_to_text``.

    Аргументы:
        message: одно сообщение треда. Не-словарь даёт ``""``.

    Возвращает:
        Одна строка — текстовый снимок сообщения для промпта суммаризатора и ``len()`` в метриках.
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
    content = _message_content_to_text(message.get("content"))
    return f"[{role}] {content}".strip()


def message_raw_payload_chars_for_trigger(message: Dict[str, Any]) -> int:
    """
    Оценка длины содержимого сообщения для порога суммаризации (§1): без префиксов ``[role]``, без склейки в промпт.

    Суммируются поля полезной нагрузки (текст content, имя и аргументы вызова, вывод инструмента) через
    ``_message_content_to_text``; служебные обёртки вида ``[function_call]`` не включаются.
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
    return len(_message_content_to_text(message.get("content")))


def calc_history_chars_for_trigger(messages: List[Dict[str, Any]]) -> int:
    """Суммарный объём истории в «сырых» символах для сравнения с ``chat_completions_summary_trigger_chars``."""
    return sum(message_raw_payload_chars_for_trigger(m) for m in messages)


def calc_messages_chars_total(messages: List[Dict[str, Any]]) -> int:
    """
    Считает суммарную длину в символах сериализованного трека (каноничный вид для промпта LLM).

    Зачем: метрики до/после схлопывания и согласование с оценкой длины в ``dialog_history``; **не** используется для
    порога запуска (см. ``calc_history_chars_for_trigger``).

    Что делает: для каждого элемента списка берёт ``len(message_text_for_summary(message))`` и суммирует.

    Аргументы:
        messages: список сообщений того же вида, что обходит pipeline суммаризации.

    Возвращает:
        Неотрицательное целое — суммарное число символов по всем сообщениям.
    """
    total = 0
    for message in messages:
        total += len(message_text_for_summary(message))
    return total
