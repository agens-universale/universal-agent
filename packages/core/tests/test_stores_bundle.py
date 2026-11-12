"""Связка ``Stores``: треды и ресурсы агента — два независимых контракта,
которые встраивающий передаёт одним объектом (``stores=``)."""

from types import SimpleNamespace

import pytest
from gc_core.aio import agent as aio_agent
from gc_core.aio.agent import AsyncAgent
from gc_core.stores import Stores
from gc_testkit.memory_agent_store import MemoryAgentStore
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from gc_testkit.settings import make_settings


class _ClosingThreads(MemoryThreadStore):
    def __init__(self, log):
        super().__init__()
        self.log = log

    async def close(self):
        self.log.append("threads")


class _ClosingAgent(MemoryAgentStore):
    def __init__(self, log):
        super().__init__(fallback=None)
        self.log = log

    async def close(self):
        self.log.append("agent")


async def test_aclose_closes_both():
    log = []
    await Stores(
        threads=_ClosingThreads(log), agent=_ClosingAgent(log)
    ).aclose()
    assert log == ["threads", "agent"]


async def test_one_object_for_both_is_closed_once():
    log = []

    async def close():
        log.append("both")

    both = SimpleNamespace(close=close)
    await Stores(threads=both, agent=both).aclose()
    assert log == ["both"]


async def test_store_without_close_is_fine():
    await Stores(
        threads=MemoryThreadStore(), agent=MemoryAgentStore(fallback=None)
    ).aclose()


async def test_create_rejects_stores_together_with_a_separate_store():
    stores = Stores(MemoryThreadStore(), MemoryAgentStore(fallback=None))
    with pytest.raises(ValueError, match="stores"):
        await AsyncAgent.create(
            make_settings({}), stores=stores, thread_store=MemoryThreadStore()
        )
    with pytest.raises(ValueError, match="stores"):
        await AsyncAgent.create(
            make_settings({}),
            stores=stores,
            agent_store=MemoryAgentStore(fallback=None),
        )


async def test_create_unpacks_stores(monkeypatch):
    seen = {}

    async def fake_context(settings, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("стоп после сборки аргументов")

    monkeypatch.setattr(aio_agent, "acreate_run_context", fake_context)
    stores = Stores(MemoryThreadStore(), MemoryAgentStore(fallback=None))
    with pytest.raises(RuntimeError, match="стоп"):
        await AsyncAgent.create(make_settings({}), stores=stores)
    assert seen["thread_store"] is stores.threads
    assert seen["agent_store"] is stores.agent
