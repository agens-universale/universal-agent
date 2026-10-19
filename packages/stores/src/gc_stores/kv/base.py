"""Примитивы KV-хранилища: чтение с версией, условная запись, список.

Redis и S3 реализуют одно и то же: значение по ключу с версией (у S3 —
ETag, у Redis — счётчик), запись «только если ключа нет» или «только если
версия та», упорядоченный список ключей по префиксу. Движок держит на них
инвариант: точка фиксации записи в тред — условная запись его заголовка.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

from gc_stores.kv.keys import SCHEMA


@dataclass(frozen=True)
class Versioned:
    data: bytes
    version: str


class KV(ABC):
    """Ключ → байты с версией."""

    @abstractmethod
    async def get(self, key: str) -> Versioned | None:
        """Значение и версия; нет ключа — ``None``."""

    @abstractmethod
    async def put(
        self, key: str, data: bytes, *, if_version: str | None
    ) -> str | None:
        """Условная запись: ``if_version=None`` — только если ключа нет,
        иначе — только если версия та. Новая версия или ``None`` — условие
        не выполнено (кто-то успел раньше)."""

    @abstractmethod
    async def put_any(self, key: str, data: bytes) -> str:
        """Безусловная запись (неизменяемые объекты под уникальным ключом)."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Удалить; нет ключа — ничего."""

    @abstractmethod
    async def scan(
        self, prefix: str, *, start_after: str = "", limit: int = 1000
    ) -> list[str]:
        """Ключи с префиксом по возрастанию (байты UTF-8), после
        ``start_after``, не больше ``limit``."""

    async def check(self) -> None:  # noqa: B027  # хук: по умолчанию проверять нечего
        """Проверка возможностей при первом обращении (S3: условная
        запись); отказ — понятное исключение."""

    async def close(self) -> None:  # noqa: B027  # хук: у MemoryKV закрывать нечего
        """Закрыть подключение; повтор безвреден."""


#: Формат раскладки ключей KV (``kv/keys.py``): меняется — новый номер.
FORMAT = 1


async def read_format(kv: KV, *, required: bool) -> int:
    """Номер формата ключей хранилища (0 — не записан); ``required`` —
    расхождение с кодом или его отсутствие поднимают ``SchemaMismatch``."""
    from gc_stores.migrations import mismatch

    found = await kv.get(SCHEMA)
    current = 0 if found is None else int(json.loads(found.data)["format"])
    error = mismatch(current, FORMAT)
    if error is not None and (required or current > FORMAT):
        raise error
    return current


async def ensure_format(kv: KV) -> None:
    """Записать номер формата, если его нет; формат новее кода — отказ."""
    if await read_format(kv, required=False) == 0:
        record = json.dumps({"format": FORMAT}).encode("utf-8")
        await kv.put(SCHEMA, record, if_version=None)
    await read_format(kv, required=True)
