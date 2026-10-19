"""Хранилища на Redis (KV-движок): конформанс тредов и агента на живом
Redis, условная запись под ``WATCH``, драйвер по extra, общее подключение."""

import asyncio
import builtins

import pytest
from gc_core.threads.models import LeaseRequest, ThreadKey
from gc_stores.redis import RedisAgentStore, RedisThreadStore, open_stores
from gc_stores.redis.kv import RedisKV
from gc_testkit.agent_store_conformance import (
    PROJECT,
    assert_conforms as assert_agent_conforms,
    seed_store,
)
from gc_testkit.thread_store_conformance_v2 import CHECKS, assert_conforms

KEY = ThreadKey(system="s", thread="t")


def test_redis_thread_store_conforms(redis_url, redis_prefixes):
    def make(clock):
        return RedisThreadStore(
            redis_url, prefix=redis_prefixes.new(), clock=clock
        )

    assert assert_conforms(make) == [check.__name__ for check in CHECKS]


async def test_redis_agent_store_conforms(redis_url, redis_prefixes):
    async def make(seed):
        store = RedisAgentStore(
            redis_url, prefix=redis_prefixes.new(), agent_id="a"
        )
        await seed_store(store, seed, project=PROJECT)
        return store

    await assert_agent_conforms(make)


async def test_conditional_put_loses_to_a_concurrent_writer(
    redis_url, redis_prefixes
):
    kv = RedisKV(redis_url, prefix=redis_prefixes.new())
    try:
        version = await kv.put("k", b"0", if_version=None)
        outcomes = await asyncio.gather(
            kv.put("k", b"a", if_version=version),
            kv.put("k", b"b", if_version=version),
        )
        assert sum(o is not None for o in outcomes) == 1
        assert await kv.put("k", b"x", if_version=None) is None
        await kv.delete("k")
        assert await kv.get("k") is None
        assert await kv.scan("") == []
    finally:
        await kv.close()


async def test_scan_orders_bytes_and_pages(redis_url, redis_prefixes):
    kv = RedisKV(redis_url, prefix=redis_prefixes.new())
    try:
        for key in ("p/b", "p/a", "p/я", "q/a", "p/c"):
            await kv.put_any(key, b"")
        assert await kv.scan("p/") == ["p/a", "p/b", "p/c", "p/я"]
        assert await kv.scan("p/", start_after="p/a", limit=1) == ["p/b"]
    finally:
        await kv.close()


async def test_open_stores_share_one_client(redis_url, redis_prefixes):
    stores = open_stores(redis_url, agent_id="a", prefix=redis_prefixes.new())
    try:
        assert stores.threads._kv is stores.agent._kv
        await stores.agent.put_resource("prompt", "system", b"S")
        assert await stores.agent.read("prompt", "system") == b"S"
        record = await stores.threads.claim(KEY, LeaseRequest(owner="w"))
        assert record.lease is not None
    finally:
        await stores.aclose()
        await stores.aclose()


def test_missing_driver_names_the_extra(monkeypatch):
    real = builtins.__import__

    def fake(name, *args, **kwargs):
        if name == "redis" or name.startswith("redis."):
            raise ImportError("нет redis")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)
    store = RedisThreadStore("redis://127.0.0.1:1/0")
    with pytest.raises(ImportError, match=r"gc-stores\[redis\]"):
        asyncio.run(store.get_thread(KEY))
