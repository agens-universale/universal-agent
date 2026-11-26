"""Битый вызов тула в истории, уходящей на бэкенд: чем его заменить."""

from __future__ import annotations

import json
from typing import Callable, Dict, Iterable

from gc_core.message_policy.chat_completions import tool_output_call_id
from gc_core.message_policy.user_message_policy import (
    build_invalid_tool_call_feedback_message,
)


def _answered_call_ids(history: Iterable) -> set[str]:
    answered: set[str] = set()
    for batch in history:
        for message in batch or []:
            call_id = (
                tool_output_call_id(message)
                if isinstance(message, dict)
                else None
            )
            if call_id:
                answered.add(call_id)
    return answered


def outbound_invalid_tool_call(history: Iterable) -> Callable[[Dict], Dict]:
    """Фабрика замены битого вызова; ``history`` — батчи, где ищут выводы.

    Без записанного вывода — техническая реплика вместо вызова. С выводом
    пара остаётся, а сырой обрезок или не-объект бэкенд разбирает и отбивает
    400 на каждом ходе — аргументы уходят как ``{}``, ошибку модель видит в
    выводе вызова. Словарь от бэкенда — уже исполненные аргументы: уходят
    своим JSON. Выводы ищутся лениво, на первом битом вызове.
    """
    answered: set[str] | None = None

    def factory(message: Dict) -> Dict:
        nonlocal answered
        if answered is None:
            answered = _answered_call_ids(history)
        if message.get("call_id") not in answered:
            return build_invalid_tool_call_feedback_message(message)
        arguments = message.get("arguments")
        if isinstance(arguments, dict):
            dumped = json.dumps(arguments, ensure_ascii=False)
            return {**message, "arguments": dumped}
        return {**message, "arguments": "{}"}

    return factory
