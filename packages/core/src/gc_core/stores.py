"""Связка хранилищ агента: треды и ресурсы — два независимых контракта.

Сочетания любые: треды в базе, ресурсы на диске, или всё в одной базе::

    Stores(threads=PostgresThreadStore(dsn), agent=FileAgentStore(settings))
    stores = gc_stores.postgres.open_stores(dsn, agent_id="coder")

Связку принимают ``AsyncAgent.create(settings, stores=…)`` и плагин
Temporal (``AgentPlugin(…, stores=…)``). Закрывает её владелец —
``await stores.aclose()``.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from gc_core.agent_store import AgentStore
from gc_core.threads.store import ThreadStore


@dataclass(frozen=True)
class Stores:
    """Хранилище тредов и хранилище агента одним объектом."""

    threads: ThreadStore
    agent: AgentStore

    async def aclose(self) -> None:
        """Закрыть оба (``close()``, если есть); один объект на обе роли —
        один раз. Общий пул двух разных объектов хранилища закрывают сами
        (повторное закрытие пула безвредно)."""
        closed: list[object] = []
        for store in (self.threads, self.agent):
            if any(store is seen for seen in closed):
                continue
            closed.append(store)
            close = getattr(store, "close", None)
            if inspect.iscoroutinefunction(close):
                await close()


__all__ = ["Stores"]
