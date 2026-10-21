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

from gc_core.threads.errors import StoreUnavailable

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

    def unavailable(self, exc: BaseException) -> bool:
        """Соединения нет или оно оборвалось — запись повторит вызывающий
        (``StoreUnavailable``)."""
        return isinstance(exc, (ConnectionError, TimeoutError))


class GuardedKV(KV):
    """KV, у которого обрыв соединения драйвера — ``StoreUnavailable``.
    Остальные атрибуты — как у обёрнутого."""

    def __init__(self, inner: KV) -> None:
        self.inner = inner

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    async def _call(self, call):
        try:
            return await call
        except Exception as exc:
            if self.inner.unavailable(exc):
                message = str(exc) or type(exc).__name__
                raise StoreUnavailable(message) from exc
            raise

    async def get(self, key: str) -> Versioned | None:
        return await self._call(self.inner.get(key))

    async def put(
        self, key: str, data: bytes, *, if_version: str | None
    ) -> str | None:
        return await self._call(
            self.inner.put(key, data, if_version=if_version)
        )

    async def put_any(self, key: str, data: bytes) -> str:
        return await self._call(self.inner.put_any(key, data))

    async def delete(self, key: str) -> None:
        await self._call(self.inner.delete(key))

    async def scan(
        self, prefix: str, *, start_after: str = "", limit: int = 1000
    ) -> list[str]:
        return await self._call(
            self.inner.scan(prefix, start_after=start_after, limit=limit)
        )

    async def check(self) -> None:
        await self._call(self.inner.check())

    async def close(self) -> None:
        await self.inner.close()

    def unavailable(self, exc: BaseException) -> bool:
        return self.inner.unavailable(exc)


def guarded(kv: KV) -> KV:
    """Обернуть один раз: обёрнутый возвращается как есть."""
    return kv if isinstance(kv, GuardedKV) else GuardedKV(kv)


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
