"""Запись «итог хода» в файле треда: схема v1 и её разбор.

Строка ``data_*.jsonl`` с ``type: "turn_outcome"``; полезная нагрузка —
под ключом ``outcome`` (не ``payload``: читатели считают ``payload``
сообщением). Модуль — только формат: фактов не вычисляет, времени
«сейчас» не знает. Схема расширяется без новой версии только
необязательными полями — разбор лишние ключи игнорирует.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, get_args

TURN_OUTCOME_TYPE = "turn_outcome"
TURN_OUTCOME_VERSION = 1

TurnStatus = Literal["done", "stopped", "error"]
USER_STOP = "user_stop"
# Ход снят отменой задачи без запроса стопа (таймаут, остановка приложения).
CANCELLED = "cancelled"

_STATUSES = frozenset(get_args(TurnStatus))
_OPTIONAL_TEXT = ("reason", "opened_by")
_REQUIRED_TEXT = ("started_at", "ended_at")


@dataclass(frozen=True)
class TurnOutcome:
    """Итог закрытого хода — доменные факты без слов ленты."""

    status: TurnStatus
    reason: str | None
    opened_by: str | None
    started_at: str
    ended_at: str
    duration_sec: float
    error: Mapping[str, str] | None
    # Время завершившихся вызовов хода без ожидания человека, вложенные
    # субагента — в той же карте. Необязательное поле той же версии.
    calls: Mapping[str, float] | None = None

    def to_payload(self) -> dict[str, Any]:
        """Значение ключа ``outcome`` записи (с версией схемы)."""
        payload = {
            "v": TURN_OUTCOME_VERSION,
            "status": self.status,
            "reason": self.reason,
            "opened_by": self.opened_by,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_sec": self.duration_sec,
            "error": None if self.error is None else dict(self.error),
        }
        if self.calls:
            payload["calls"] = dict(self.calls)
        return payload


def is_turn_outcome_record(record: Mapping[str, Any]) -> bool:
    """Строка файла треда — запись итога хода."""
    return record.get("type") == TURN_OUTCOME_TYPE


def parse_turn_outcome(record: Mapping[str, Any]) -> TurnOutcome | None:
    """Итог из строки файла треда; чужая или кривая запись — ``None``."""
    if not is_turn_outcome_record(record):
        return None
    payload = record.get("outcome")
    if not isinstance(payload, Mapping) or not _is_valid(payload):
        return None
    error = payload["error"]
    return TurnOutcome(
        status=payload["status"],
        reason=payload["reason"],
        opened_by=payload["opened_by"],
        started_at=payload["started_at"],
        ended_at=payload["ended_at"],
        duration_sec=float(payload["duration_sec"]),
        error=None if error is None else dict(error),
        calls=_calls_or_none(payload.get("calls")),
    )


def _calls_or_none(value: Any) -> dict[str, float] | None:
    """Карта ``calls``; кривое значение — как отсутствующее (запись живёт)."""
    if not isinstance(value, Mapping) or not value:
        return None
    valid = all(
        isinstance(k, str) and _is_number(v) and v >= 0
        for k, v in value.items()
    )
    return dict(value) if valid else None


def _is_valid(payload: Mapping[str, Any]) -> bool:
    return (
        payload.get("v") == TURN_OUTCOME_VERSION
        and payload.get("status") in _STATUSES
        and all(_is_text_or_none(payload, k) for k in _OPTIONAL_TEXT)
        and all(isinstance(payload.get(k), str) for k in _REQUIRED_TEXT)
        and _is_number(payload.get("duration_sec"))
        and _is_error(payload.get("error", ...))
    )


def _is_text_or_none(payload: Mapping[str, Any], key: str) -> bool:
    return key in payload and (
        payload[key] is None or isinstance(payload[key], str)
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_error(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, Mapping) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )
