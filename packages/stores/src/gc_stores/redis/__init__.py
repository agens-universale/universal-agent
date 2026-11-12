"""Хранилища на Redis: ``RedisThreadStore``, ``RedisAgentStore``.

Драйвер — redis (``redis.asyncio``, extra ``gc-stores[redis]``); импорт
модуля его не требует, отказ без драйвера — при первом обращении.
"""

from gc_stores.redis.store import (
    RedisAgentStore,
    RedisThreadStore,
    open_stores,
)

__all__ = ["RedisAgentStore", "RedisThreadStore", "open_stores"]
