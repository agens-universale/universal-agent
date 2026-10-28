"""Идемпотентность запросов к задачам (спека сервера, 14.2).

Ключ — заголовок ``Idempotency-Key`` или ``webhook-id`` (Standard Webhooks).
Из него выводятся ``op_id`` записи во входящий поток и id сообщения, времени
в данных нет: повтор с тем же телом — та же квитанция хранилища и тот же
``message_seq``, а тот же ключ с другим телом хранилище отвергает
(``OperationMismatch`` → 422). Окно — срок хранения квитанций
(``webhook.idempotency_ttl_hours``), их чистит уборщик сервера.
"""

from __future__ import annotations

import hashlib
from typing import Any

HEADERS = ("idempotency-key", "webhook-id")


def idempotency_key(headers: Any) -> str | None:
    for name in HEADERS:
        value = (headers.get(name) or "").strip()
        if value:
            return value
    return None


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def operation_id(key: str) -> str:
    return "idem_" + _digest(key)


def message_id(key: str) -> str:
    return "m_idem_" + _digest(key)[:32]


def task_id_for_key(key: str) -> str:
    """Задача запроса с ключом идемпотентности, но без ``task_id``: повтор
    попадает в ту же задачу."""
    return "idem-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
