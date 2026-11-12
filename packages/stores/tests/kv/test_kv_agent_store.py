"""``KvAgentStore`` на ``MemoryKV``: конформанс ``AgentStore``, разделение
агентов, метка версии умолчаний, гонка записи памяти."""

import asyncio
import logging

import pytest
from gc_core.agent_store import MemoryConflict, ProjectKey
from gc_stores.kv.agent_store import KvAgentStore
from gc_stores.kv.memory import MemoryKV
from gc_testkit.agent_store_conformance import (
    PROJECT,
    assert_conforms,
    seed_store,
)


async def test_kv_agent_store_conforms():
    async def make(seed):
        store = KvAgentStore(MemoryKV(), agent_id="a", backend="memory")
        await seed_store(store, seed, project=PROJECT)
        return store

    await assert_conforms(make)


async def test_two_agents_on_one_kv_do_not_see_each_other():
    kv = MemoryKV()
    a = KvAgentStore(kv, agent_id="a", backend="memory")
    b = KvAgentStore(kv, agent_id="b", backend="memory")
    await a.put_resource("prompt", "system", b"A")
    await a.write_memory(PROJECT, "память a", expected_version=None)
    assert await b.read("prompt", "system") is None
    assert await b.read_memory(PROJECT) is None
    assert [r.name for r in await b.list_resources()] == []


async def test_stale_bundled_default_warns_once(caplog):
    store = KvAgentStore(MemoryKV(), agent_id="a", backend="memory")
    await store.put_resource("prompt", "system", b"S", source="bundled@0.0.1")
    caplog.set_level(logging.WARNING)
    await store.read("prompt", "system")
    await store.read("prompt", "system")
    hints = [r for r in caplog.records if "--with-defaults" in r.getMessage()]
    assert len(hints) == 1


async def test_concurrent_first_memory_writes_one_conflict():
    store = KvAgentStore(MemoryKV(), agent_id="a", backend="memory")
    outcomes = await asyncio.gather(
        store.write_memory(PROJECT, "раз", expected_version=None),
        store.write_memory(PROJECT, "два", expected_version=None),
        return_exceptions=True,
    )
    assert sum(isinstance(o, MemoryConflict) for o in outcomes) == 1


async def test_project_resources_are_per_project_and_listed():
    store = KvAgentStore(MemoryKV(), agent_id="a", backend="memory")
    await store.put_resource("skill", "project/x/SKILL.md", b"X", project="p1")
    await store.put_resource("prompt", "system", b"S")
    assert (
        await store.read(
            "skill", "project/x/SKILL.md", project=ProjectKey("p1")
        )
        == b"X"
    )
    assert (
        await store.read(
            "skill", "project/x/SKILL.md", project=ProjectKey("p2")
        )
        is None
    )
    listed = {
        (r.kind, r.name, r.project)
        for r in await store.list_resources(project="p1")
    }
    assert listed == {
        ("prompt", "system", None),
        ("skill", "project/x/SKILL.md", "p1"),
    }
    with pytest.raises(NotADirectoryError):
        await store.list(
            "skill", "project/x/SKILL.md", project=ProjectKey("p1")
        )
    await store.delete_resource("prompt", "system")
    assert await store.read("prompt", "system") is None


async def test_resource_left_without_manifest_entry_is_picked_up(
    monkeypatch,
):
    """Процесс упал между записью ресурса и манифеста: ресурс всё равно в
    списке. Источник записан вместе с байтами, поэтому ресурс человека
    сохраняет защиту от ``sync --with-defaults`` даже без манифеста."""
    import hashlib

    from gc_stores.sync import copy_defaults

    kv = MemoryKV()
    crashed = KvAgentStore(kv, agent_id="a", backend="memory")

    async def died(*_args, **_kwargs):
        raise RuntimeError("процесс упал")

    monkeypatch.setattr(crashed, "_update_manifest", died)
    with pytest.raises(RuntimeError):
        await crashed.put_resource("prompt", "summary", b"own")
    store = KvAgentStore(kv, agent_id="a", backend="memory")
    listed = {(r.kind, r.name): r for r in await store.list_resources()}
    picked = listed[("prompt", "summary")]
    assert picked.source is None  # источник сохранён атомарно с содержимым
    assert picked.version == hashlib.sha256(b"own").hexdigest()
    await copy_defaults(store)
    assert await store.read("prompt", "summary") == b"own"


async def test_manifest_entry_without_resource_is_dropped():
    kv = MemoryKV()
    store = KvAgentStore(kv, agent_id="a", backend="memory")
    await store.put_resource("prompt", "summary", b"x")
    await store.put_resource("skill", "project/s/SKILL.md", b"s", project="p")
    # Ресурс удалён, а запись манифеста осталась (упали посередине).
    await kv.delete(store._resource("_", "prompt", "summary"))
    assert [r.name for r in await store.list_resources()] == []
    listed = await store.list_resources(project="p")
    assert [r.name for r in listed] == ["project/s/SKILL.md"]
    manifest, _version = await store._manifest()
    assert list(manifest) == [next(iter(manifest))]
    assert "summary" not in next(iter(manifest))
