"""Запись «смена модели» в файле треда: схема v1 и её разбор.

Строка ``data_*.jsonl`` с ``type: "model_choice"``; полезная нагрузка —
под ключом ``model`` (не ``payload``: читатели считают ``payload``
сообщением). Не сообщение и вне контекста модели: по ней лента после
восстановления рисует черту смены на своём месте — за сообщением ``seq``
конверта. Модуль — только формат. Схема расширяется без новой версии
только необязательными полями — разбор лишние ключи игнорирует.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

MODEL_CHOICE_TYPE = "model_choice"
MODEL_CHOICE_VERSION = 1


@dataclass(frozen=True)
class ModelChoiceRecord:
    """Применённая смена выбора модели главного трейса.

    ``choice`` — выбор (`auto`, слово уровня или алиас); ``alias`` —
    модель, которой он пошёл (``None`` — модели нет: `auto` на завершении
    рана); ``before`` — выбор, действовавший до смены (``None`` —
    неизвестен)."""

    choice: str
    alias: str | None
    before: str | None

    def to_payload(self) -> dict[str, Any]:
        """Значение ключа ``model`` записи (с версией схемы)."""
        return {
            "v": MODEL_CHOICE_VERSION,
            "choice": self.choice,
            "alias": self.alias,
            "before": self.before,
        }


def is_model_choice_record(record: Mapping[str, Any]) -> bool:
    """Строка файла треда — запись смены модели."""
    return record.get("type") == MODEL_CHOICE_TYPE


def parse_model_choice(record: Mapping[str, Any]) -> ModelChoiceRecord | None:
    """Смена из строки файла треда; чужая или кривая запись — ``None``."""
    if not is_model_choice_record(record):
        return None
    payload = record.get("model")
    if not isinstance(payload, Mapping) or not _is_valid(payload):
        return None
    return ModelChoiceRecord(
        choice=payload["choice"],
        alias=payload.get("alias"),
        before=payload.get("before"),
    )


def _is_valid(payload: Mapping[str, Any]) -> bool:
    choice = payload.get("choice")
    return (
        payload.get("v") == MODEL_CHOICE_VERSION
        and isinstance(choice, str)
        and bool(choice)
        and all(_is_text_or_none(payload.get(k)) for k in ("alias", "before"))
    )


def _is_text_or_none(value: Any) -> bool:
    return value is None or isinstance(value, str)
