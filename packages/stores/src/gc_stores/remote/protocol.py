"""Протокол прокси одного треда: запрос и ответ — по одной строке JSON на
соединение (unix-сокет).

Запрос — ``{"token", "op", "key": {system, thread}, "args"}``, ответ —
``{"ok": …}`` или ``{"error": {"type", "message"}}``. Модели контракта
идут через ``wire``: только заданные поля — контракт держится на
«заданности» (запись без ``title`` не стирает заголовок), и полный дамп
сделал бы заданными все поля; NaN и бесконечности в сообщениях доходят
как есть (JSON-режим pydantic заменил бы их на ``null``). Байты — base64.
Отказы контракта
доходят до клиента своим типом: сессия по типу решает, повторить запись
или остановить ход.
"""

from __future__ import annotations

import json
from typing import Any

from gc_core.threads.errors import (
    InvalidCursor,
    OperationMismatch,
    OwnershipError,
    StoreUnavailable,
    ThreadBusy,
    ThreadConflict,
    ThreadStoreError,
)
from pydantic import BaseModel
from pydantic_core import to_jsonable_python

#: Предел строки ответа сервера (история треда, вложение в base64).
FRAME_LIMIT = 64 * 1024 * 1024
#: Предел строки запроса песочницы: его держит в памяти сервер, на каждое
#: соединение (вложение до ~12 МиБ в base64).
REQUEST_LIMIT = 16 * 1024 * 1024
#: Тип отказа «не ваша операция или не ваш тред».
FORBIDDEN = "Forbidden"

ERRORS: dict[str, type[Exception]] = {
    cls.__name__: cls
    for cls in (
        ThreadConflict,
        ThreadBusy,
        OwnershipError,
        OperationMismatch,
        StoreUnavailable,
        InvalidCursor,
        ValueError,
        FileNotFoundError,
    )
}


def wire(model: BaseModel) -> Any:
    """Модель контракта в JSON-значение кадра."""
    return to_jsonable_python(
        model.model_dump(exclude_unset=True), inf_nan_mode="constants"
    )


def encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"


def decode(line: bytes) -> dict[str, Any]:
    message = json.loads(line.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("кадр протокола — не объект JSON")
    return message


def error_of(payload: dict[str, Any]) -> Exception:
    """Исключение клиента по отказу сервера."""
    kind = str(payload.get("type") or "")
    message = str(payload.get("message") or "")
    if kind == FORBIDDEN:
        return PermissionError(message)
    return ERRORS.get(kind, ThreadStoreError)(message)
