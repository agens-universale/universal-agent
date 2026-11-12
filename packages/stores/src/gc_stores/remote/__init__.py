"""Хранилище тредов через прокси одного треда (песочница swarm).

В ``config.yml`` песочницы::

    storage:
      threads:
        type: gc_stores.remote:open
        socket: ${ENV:GC_THREAD_SOCKET}
        token: ${ENV:GC_THREAD_TOKEN}
"""

from __future__ import annotations

from typing import Any

from gc_stores.remote.thread_store import RemoteThreadStore


def open(params: dict[str, Any]) -> RemoteThreadStore:  # noqa: A001  # имя фабрики из storage.threads.type
    """Фабрика для ``storage.threads.type: gc_stores.remote:open``."""
    socket, token = params.get("socket"), params.get("token")
    if not isinstance(socket, str) or not socket:
        raise ValueError("storage.threads.socket: нужен путь к сокету прокси")
    if not isinstance(token, str) or not token:
        raise ValueError("storage.threads.token: нужен токен задачи")
    return RemoteThreadStore(socket, token)


__all__ = ["RemoteThreadStore", "open"]
