"""Хранилища на S3: треды (``S3ThreadStore``) и агент (``S3AgentStore``)
— KV-движок поверх ``S3KV``.

Только S3 с условной записью (AWS S3, MinIO): хранилище проверяет её при
первом обращении. Цена: запись хода — несколько PUT (куски, заголовок,
ключ индекса), список тредов — GET заголовка на каждую строку страницы.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from gc_core.stores import Stores

from gc_stores.kv.agent_store import KvAgentStore
from gc_stores.kv.thread_store import KvThreadStore
from gc_stores.s3.kv import S3KV


def _kv(bucket: str | None, kv: S3KV | None, options: dict[str, Any]) -> S3KV:
    if kv is not None:
        if bucket is not None:
            raise ValueError("нужен ровно один из bucket и kv")
        return kv
    if bucket is None:
        raise ValueError("нужен bucket (или kv)")
    return S3KV(bucket, **options)


class S3ThreadStore(KvThreadStore):
    """Треды в бакете S3 ``bucket`` под ``prefix``; ``kv`` — общее
    подключение (``open_stores``)."""

    def __init__(
        self,
        bucket: str | None = None,
        *,
        kv: S3KV | None = None,
        prefix: str = "",
        endpoint_url: str | None = None,
        region: str | None = None,
        client_options: dict[str, Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        options = {
            "prefix": prefix,
            "endpoint_url": endpoint_url,
            "region": region,
            "client_options": client_options,
        }
        super().__init__(_kv(bucket, kv, options), clock=clock)


class S3AgentStore(KvAgentStore):
    """Ресурсы и память агента ``agent_id`` в бакете S3."""

    def __init__(
        self,
        bucket: str | None = None,
        *,
        agent_id: str,
        kv: S3KV | None = None,
        prefix: str = "",
        endpoint_url: str | None = None,
        region: str | None = None,
        client_options: dict[str, Any] | None = None,
    ) -> None:
        options = {
            "prefix": prefix,
            "endpoint_url": endpoint_url,
            "region": region,
            "client_options": client_options,
        }
        super().__init__(
            _kv(bucket, kv, options), agent_id=agent_id, backend="s3"
        )


def open_stores(
    bucket: str,
    *,
    agent_id: str,
    prefix: str = "",
    endpoint_url: str | None = None,
    region: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Stores:
    """Треды и хранилище агента ``agent_id`` на одном клиенте S3."""
    kv = S3KV(bucket, prefix=prefix, endpoint_url=endpoint_url, region=region)
    return Stores(
        threads=S3ThreadStore(kv=kv, clock=clock),
        agent=S3AgentStore(kv=kv, agent_id=agent_id),
    )


__all__ = ["S3AgentStore", "S3ThreadStore", "open_stores"]
