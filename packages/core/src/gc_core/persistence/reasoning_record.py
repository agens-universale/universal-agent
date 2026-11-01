"""Запись «мысль модели» треда: схема v1 и её разбор.

Запись потока ядра ``reasoning`` — ``{"after": id, "thought": …}``:
мысль за сообщением ``after`` (последним записанным на момент записи).
Читатели ленты получают её конвертом с ``type: "thought"`` (не
``"reasoning"``: этот тип подавляют ``trace_mapper`` и экспорт как items
Responses); полезная нагрузка — под ключом ``thought`` (не ``payload``:
читатели считают ``payload`` сообщением). Не сообщение и вне контекста
модели. Модуль — только формат. Схема расширяется без новой версии
только необязательными полями — разбор лишние ключи игнорирует.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

THOUGHT_TYPE = "thought"
THOUGHT_VERSION = 1


@dataclass(frozen=True)
class ReasoningRecord:
    """Мысль модели одного эпизода: текст, длительность в секундах
    (``None`` — неизвестна) и попытка вызова (``None`` — не задана)."""

    text: str
    duration_sec: float | None
    attempt: str | None

    def to_payload(self) -> dict[str, Any]:
        """Значение ключа ``thought`` записи (с версией схемы)."""
        return {
            "v": THOUGHT_VERSION,
            "text": self.text,
            "duration_sec": self.duration_sec,
            "attempt": self.attempt,
        }


def reasoning_entry(
    record: ReasoningRecord, *, after: str | None
) -> dict[str, Any]:
    """Запись потока ``reasoning``: мысль за сообщением ``after``."""
    return {"after": after, "thought": record.to_payload()}


def is_reasoning_record(record: Mapping[str, Any]) -> bool:
    """Конверт записи треда — запись мысли модели."""
    return record.get("type") == THOUGHT_TYPE


def parse_reasoning(record: Mapping[str, Any]) -> ReasoningRecord | None:
    """Мысль из конверта записи; чужая или кривая запись — ``None``."""
    if not is_reasoning_record(record):
        return None
    payload = record.get("thought")
    if not isinstance(payload, Mapping) or not _is_valid(payload):
        return None
    return ReasoningRecord(
        text=payload["text"],
        duration_sec=payload.get("duration_sec"),
        attempt=payload.get("attempt"),
    )


def _is_valid(payload: Mapping[str, Any]) -> bool:
    text = payload.get("text")
    return (
        payload.get("v") == THOUGHT_VERSION
        and isinstance(text, str)
        and bool(text)
        and _is_number_or_none(payload.get("duration_sec"))
        and _is_text_or_none(payload.get("attempt"))
    )


def _is_number_or_none(value: Any) -> bool:
    return value is None or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    )


def _is_text_or_none(value: Any) -> bool:
    return value is None or isinstance(value, str)
