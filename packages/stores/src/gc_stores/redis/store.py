"""Хранилища на Redis: треды (``RedisThreadStore``) и агент
(``RedisAgentStore``) — KV-движок поверх ``RedisKV``.

«Быстрое хранилище для простых задач»: атомарность записи в тред
гарантирована (условная запись заголовка под ``WATCH``), долговечность — нет
(перезапуск без AOF ``appendfsync always`` теряет последние ходы,
переключение реплики — подтверждённые записи). Redis Cluster не
поддерживается: одиночный Redis или Sentinel.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from gc_core.stores import Stores

from gc_stores.kv.agent_store import KvAgentStore
from gc_stores.kv.base import guarded
from gc_stores.kv.thread_store import KvThreadStore
from gc_stores.redis.kv import RedisKV


class RedisThreadStore(KvThreadStore):
    """Треды в Redis: ровно одно из ``url``, ``client`` и ``kv`` (общее
    подключение ``open_stores``); ``prefix`` — пространство ключей."""

    def __init__(
        self,
        url: str | None = None,
        *,
        client: Any = None,
        kv: RedisKV | None = None,
        prefix: str = "gc:",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if kv is None:
            kv = RedisKV(url, client=client, prefix=prefix)
        elif url is not None or client is not None:
            raise ValueError("нужен ровно один из url, client и kv")
        super().__init__(kv, clock=clock)


class RedisAgentStore(KvAgentStore):
    """Ресурсы и память агента ``agent_id`` в Redis."""

    def __init__(
        self,
        url: str | None = None,
        *,
        agent_id: str,
        client: Any = None,
        kv: RedisKV | None = None,
        prefix: str = "gc:",
    ) -> None:
        if kv is None:
            kv = RedisKV(url, client=client, prefix=prefix)
        elif url is not None or client is not None:
            raise ValueError("нужен ровно один из url, client и kv")
        super().__init__(kv, agent_id=agent_id, backend="redis")


def open_stores(
    url: str,
    *,
    agent_id: str,
    prefix: str = "gc:",
    clock: Callable[[], datetime] | None = None,
) -> Stores:
    """Треды и хранилище агента ``agent_id`` на одном клиенте Redis."""
    kv = guarded(RedisKV(url, prefix=prefix))
    return Stores(
        threads=RedisThreadStore(kv=kv, clock=clock),
        agent=RedisAgentStore(kv=kv, agent_id=agent_id),
    )


__all__ = ["RedisAgentStore", "RedisThreadStore", "open_stores"]
