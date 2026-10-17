"""Конверт JSON-RPC 2.0 и версия протокола. Решений о задачах здесь нет."""

from __future__ import annotations

import json
from typing import Any

from gc_a2a.errors import invalid_request, parse_error

_VERSION = "1.0"


def parse_call(raw: bytes) -> dict:
    """Объект запроса. Тело не JSON — ошибка разбора, иначе конверт."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise parse_error() from exc
    if not isinstance(data, dict):
        raise invalid_request()
    request_id = _request_id(data)
    if data.get("jsonrpc") != "2.0":
        raise invalid_request(request_id)
    method = data.get("method")
    if not isinstance(method, str) or method == "" or "id" not in data:
        raise invalid_request(request_id)
    if not _valid_id(data.get("id")):
        raise invalid_request(request_id)
    return data


def negotiated_version(
    header: str | None, query: str | None, *, header_present: bool
) -> str:
    """Заголовок важнее query. Пустое и отсутствующее — не 1.0."""
    if header_present:
        return (header or "").strip()
    if query is not None:
        return query.strip()
    return ""


def version_ok(
    header: str | None, query: str | None, *, header_present: bool
) -> bool:
    return (
        negotiated_version(header, query, header_present=header_present)
        == _VERSION
    )


def _request_id(data: dict) -> Any:
    if "id" not in data or not _valid_id(data.get("id")):
        return None
    return data.get("id")


def _valid_id(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (str, int))
