"""Хранилища на S3 (KV-движок): конформанс тредов и агента на живом
S3-совместимом (MinIO), проверка условной записи при старте, драйвер по
extra, общее подключение."""

import asyncio
import builtins

import pytest
from gc_core.threads.models import LeaseRequest, ThreadKey
from gc_stores.s3 import S3AgentStore, S3ThreadStore, open_stores
from gc_stores.s3.kv import ConditionalWritesUnsupported, S3KV
from gc_testkit.agent_store_conformance import (
    PROJECT,
    assert_conforms as assert_agent_conforms,
    seed_store,
)
from gc_testkit.thread_store_conformance_v2 import CHECKS, assert_conforms
from gc_testkit.session_checks import CHECKS as SESSION_CHECKS
from gc_testkit.session_checks import aassert_session_works

KEY = ThreadKey(system="s", thread="t")


def _options(config, prefix):
    return {
        "prefix": prefix,
        "endpoint_url": config["endpoint_url"],
        "region": config["region"],
    }


def test_s3_thread_store_conforms(s3_config, s3_prefixes):
    def make(clock):
        return S3ThreadStore(
            s3_config["bucket"],
            **_options(s3_config, s3_prefixes.new()),
            clock=clock,
        )

    assert assert_conforms(make) == [check.__name__ for check in CHECKS]


async def test_session_over_s3_works(s3_config, s3_prefixes):
    def make():
        return S3ThreadStore(
            s3_config["bucket"], **_options(s3_config, s3_prefixes.new())
        )

    passed = await aassert_session_works(make)
    assert passed == [check.__name__ for check in SESSION_CHECKS]


async def test_s3_agent_store_conforms(s3_config, s3_prefixes):
    async def make(seed):
        store = S3AgentStore(
            s3_config["bucket"],
            agent_id="a",
            **_options(s3_config, s3_prefixes.new()),
        )
        await seed_store(store, seed, project=PROJECT)
        return store

    await assert_agent_conforms(make)


class _NoConditions(S3KV):
    """S3 без условной записи: запись «только если нет» проходит всегда."""

    async def put(self, key, data, *, if_version):
        return await self.put_any(key, data)


async def test_s3_without_conditional_writes_is_refused(
    s3_config, s3_prefixes
):
    kv = _NoConditions(
        s3_config["bucket"], **_options(s3_config, s3_prefixes.new())
    )
    store = S3ThreadStore(kv=kv)
    try:
        with pytest.raises(ConditionalWritesUnsupported, match="If-Match"):
            await store.get_thread(KEY)
    finally:
        await store.close()


async def test_open_stores_share_one_client(s3_config, s3_prefixes):
    stores = open_stores(
        s3_config["bucket"],
        agent_id="a",
        **_options(s3_config, s3_prefixes.new()),
    )
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
        if name == "aiobotocore" or name.startswith("aiobotocore."):
            raise ImportError("нет aiobotocore")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)
    store = S3ThreadStore("bucket", endpoint_url="http://127.0.0.1:1")
    with pytest.raises(ImportError, match=r"gc-stores\[s3\]"):
        asyncio.run(store.get_thread(KEY))
