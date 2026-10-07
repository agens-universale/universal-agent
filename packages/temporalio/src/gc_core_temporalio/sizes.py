"""Размер значения в истории Temporal — для всех пределов пакета.

JSON-конвертер Temporal пишет с ``ensure_ascii``: каждый не-ASCII символ —
``\\uXXXX`` (6 байт), то есть кириллица в истории втрое тяжелее, чем в UTF-8.
Пределы записи (2 МБ) поэтому считаются по этой форме, а не по ``encode()``.
Модуль импортирует и код workflow: только ``json`` и контракт ошибок ядра.
"""

import dataclasses
import json

from gc_core.execution.contracts import ExecutionFailure

#: Предел входа activity в истории Temporal (запись — 2 МиБ, с запасом под
#: обёртку вызова).
ACTIVITY_INPUT_LIMIT = 2_000_000


def payload_size(value) -> int:
    """Байты значения в JSON истории Temporal (ASCII: символ = байт)."""
    return len(
        json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), default=str
        )
    )


class HistoryLimitExceeded(ExecutionFailure):
    """Значение не помещается в запись истории Temporal (вход или результат
    activity, вход субагента). Отказ хода, а не сессии: сессия продолжает
    принимать сообщения."""


def check_history_size(what: str, value, limit: int = ACTIVITY_INPUT_LIMIT):
    """``HistoryLimitExceeded`` с размером, если ``value`` (dataclass — по
    полям) тяжелее ``limit`` в истории. → размер."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    size = payload_size(value)
    if size > limit:
        raise HistoryLimitExceeded(
            f"{what} — {size} байт в истории Temporal, больше лимита записи "
            f"({limit})"
        )
    return size


def cut_to_payload(text: str, limit: int) -> str:
    """Самый длинный префикс ``text`` не тяжелее ``limit`` в истории."""
    if payload_size(text) <= limit:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if payload_size(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low]


__all__ = [
    "ACTIVITY_INPUT_LIMIT",
    "HistoryLimitExceeded",
    "check_history_size",
    "cut_to_payload",
    "payload_size",
]
