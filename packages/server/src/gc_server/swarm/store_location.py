"""Где лежат данные хранилища тредов — без учётных данных.

Песочница swarm ходит в базу тредов сервера своей учётной записью. Сверка
«та же база» сравнивает адрес сервера базы (хост, порт, имя базы) и
параметры, делящие её (схема, префикс, бакет), но не логин и пароль.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

#: Параметр с адресом сервера базы по типу хранилища.
_ADDRESS = {
    "postgres": "dsn",
    "mysql": "dsn",
    "redis": "url",
    "s3": "endpoint_url",
}
#: Параметры, делящие базу, и их умолчания в gc-stores.
_PLACE: dict[str, dict[str, Any]] = {
    "postgres": {"schema": "public"},
    "mysql": {},
    "redis": {"prefix": "gc:"},
    "s3": {"bucket": None, "prefix": ""},
}
#: Порт по умолчанию по схеме адреса.
_PORTS = {
    "postgres": 5432,
    "postgresql": 5432,
    "mysql": 3306,
    "redis": 6379,
    "rediss": 6379,
    "http": 80,
    "https": 443,
}
#: Имя базы, когда в адресе его нет.
_DEFAULT_PATH = {"redis": "0"}


def location(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Место данных: тип, адрес сервера базы без учётных данных и
    параметры, делящие базу (умолчания подставлены)."""
    place: dict[str, Any] = {"type": kind}
    address = params.get(_ADDRESS.get(kind, ""))
    if address:
        place["address"] = _address(kind, str(address))
    for name, default in _PLACE.get(kind, {}).items():
        place[name] = params.get(name, default)
    return place


def describe(place: dict[str, Any]) -> str:
    return ", ".join(f"{name}={value}" for name, value in place.items())


def _address(kind: str, value: str) -> str:
    """``хост:порт/база`` — логин и пароль из адреса отброшены."""
    url = urlsplit(value)
    try:
        port = url.port
    except ValueError:
        port = None
    port = port or _PORTS.get(url.scheme.lower())
    path = url.path.strip("/") or _DEFAULT_PATH.get(kind, "")
    return f"{(url.hostname or '').lower()}:{port}/{path}"
