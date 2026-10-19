"""``RedisKV`` — примитивы KV-движка на Redis (``redis.asyncio``).

Значение — хэш ``<prefix>k:<ключ>`` с полями ``d`` (байты) и ``v`` (версия
— случайный токен: ключ, удалённый и созданный заново, не повторит
прежнюю версию). Имена ключей — в упорядоченном множестве
``<prefix>keys`` (``ZRANGEBYLEX`` даёт список по префиксу в порядке
байтов). Условная запись — ``WATCH`` хэша, сверка версии, ``MULTI``/
``EXEC``: успел другой писатель — ``EXEC`` отменяется, запись — ``None``.
Lua не нужен: правила остаются в Python.

Долговечность — как настроен Redis: без AOF перезапуск теряет последние
записи (docs/sdk/stores.md, «Redis»).
"""

from __future__ import annotations

import secrets
from typing import Any

from gc_stores.kv.base import KV, Versioned
from gc_stores.sql.database import LoopBound


def driver() -> Any:
    try:
        import redis.asyncio as aioredis
        import redis.exceptions  # noqa: F401  # WatchError — по атрибуту
    except ImportError as exc:
        raise ImportError(
            "Хранилищам Redis нужен драйвер redis: установите gc-stores с "
            "extra — gc-stores[redis] (docs/sdk/stores.md, раздел Redis)"
        ) from exc
    return aioredis


class RedisKV(KV):
    """KV в Redis по ``url`` (``redis://host:6379/0``) или на клиенте
    встраивающего ``client`` (его закрывает владелец); ``prefix`` —
    пространство ключей хранилища."""

    def __init__(
        self,
        url: str | None = None,
        *,
        client: Any = None,
        prefix: str = "gc:",
    ) -> None:
        if (url is None) == (client is None):
            raise ValueError("нужен ровно один из url и client")
        self._url = url
        self._client = client
        self._owns = client is None
        self._prefix = prefix
        self._names = f"{prefix}keys"
        self._bound = LoopBound("Redis-хранилище")

    async def _redis(self) -> Any:
        if not self._owns:
            return self._client
        if self._bound.bind():
            self._client = None
        if self._client is None:
            self._client = driver().Redis.from_url(self._url)
        return self._client

    def _hash(self, key: str) -> str:
        return f"{self._prefix}k:{key}"

    async def get(self, key: str) -> Versioned | None:
        client = await self._redis()
        data, version = await client.hmget(self._hash(key), "d", "v")
        if version is None:
            return None
        return Versioned(bytes(data), version.decode("ascii"))

    async def put(
        self, key: str, data: bytes, *, if_version: str | None
    ) -> str | None:
        client = await self._redis()
        name = self._hash(key)
        token = secrets.token_hex(8)
        async with client.pipeline(transaction=True) as pipe:
            await pipe.watch(name)
            current = await pipe.hget(name, "v")
            current = None if current is None else current.decode("ascii")
            if current != if_version:
                await pipe.unwatch()
                return None
            pipe.multi()
            pipe.hset(name, mapping={"d": data, "v": token})
            pipe.zadd(self._names, {key: 0})
            try:
                await pipe.execute()
            except driver_watch_error():
                return None
        return token

    async def put_any(self, key: str, data: bytes) -> str:
        client = await self._redis()
        token = secrets.token_hex(8)
        async with client.pipeline(transaction=True) as pipe:
            pipe.hset(self._hash(key), mapping={"d": data, "v": token})
            pipe.zadd(self._names, {key: 0})
            await pipe.execute()
        return token

    async def delete(self, key: str) -> None:
        client = await self._redis()
        async with client.pipeline(transaction=True) as pipe:
            pipe.delete(self._hash(key))
            pipe.zrem(self._names, key)
            await pipe.execute()

    async def scan(
        self, prefix: str, *, start_after: str = "", limit: int = 1000
    ) -> list[str]:
        client = await self._redis()
        base = prefix.encode("utf-8")
        if start_after and start_after >= prefix:
            low = b"(" + start_after.encode("utf-8")
        else:
            low = b"[" + base
        found = await client.zrangebylex(
            self._names, low, b"[" + base + b"\xff", start=0, num=limit
        )
        return [name.decode("utf-8") for name in found]

    async def close(self) -> None:
        """Закрыть свой клиент; чужой не трогается; повтор безвреден."""
        if not self._owns or self._client is None:
            return
        if self._bound.bind():
            self._client = None
            return
        client, self._client = self._client, None
        await client.aclose()


def driver_watch_error() -> type[Exception]:
    import redis.exceptions

    return redis.exceptions.WatchError
