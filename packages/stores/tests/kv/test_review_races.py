"""Удаление не обгоняет публикацию; ресурс и источник одной версии."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

from gc_core.threads.models import SideChange, ThreadKey
from gc_stores.kv import keys
from gc_stores.kv.agent_store import KvAgentStore
from gc_stores.kv.memory import MemoryKV
from gc_stores.kv.thread_store import ALIAS_HOLD, KvThreadStore, _Sweeper

KEY = ThreadKey(system="review", thread="a")


async def test_deleted_thread_cannot_remove_transferred_alias(monkeypatch):
    now = datetime.now(UTC)
    kv = MemoryKV()
    store = KvThreadStore(kv, clock=lambda: now)
    other = ThreadKey(system="review", thread="b")
    await store.append_side(KEY, SideChange(op_id="a", add_aliases=["shared"]))
    now += ALIAS_HOLD + timedelta(seconds=1)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = kv.delete_if

    async def delayed(name, version):
        if name == keys.alias_key("shared"):
            entered.set()
            await resume.wait()
        return await original(name, version)

    monkeypatch.setattr(kv, "delete_if", delayed)
    removing = asyncio.create_task(store.delete(KEY, op_id="delete"))
    await asyncio.wait_for(entered.wait(), 2)
    await store.append_side(
        other, SideChange(op_id="b", add_aliases=["shared"])
    )
    resume.set()
    await removing
    assert await store.resolve_alias("shared") == other


async def test_sweeper_fences_late_chunk_publication(monkeypatch):
    now = datetime.now(UTC)
    kv = MemoryKV()
    store = KvThreadStore(kv, clock=lambda: now)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = kv.put
    pause = True

    async def delayed(name, data, *, if_version):
        nonlocal pause
        if pause and name == keys.head_key(KEY):
            pause = False
            entered.set()
            await resume.wait()
        return await original(name, data, if_version=if_version)

    monkeypatch.setattr(kv, "put", delayed)
    writing = asyncio.create_task(
        store.append_side(
            KEY,
            SideChange(
                op_id="inbox",
                append=[("inbox", [{"content": "kept"}])],
            ),
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    now += timedelta(hours=2)
    assert await _Sweeper(store, now - timedelta(hours=1)).chunks() == 1
    resume.set()
    await writing
    assert [
        e.data["content"] for e in await store.read_stream(KEY, "inbox")
    ] == ["kept"]


async def test_late_manifest_cannot_lie_about_resource_contents(monkeypatch):
    store = KvAgentStore(MemoryKV(), agent_id="a", backend="memory")
    entered, resume = asyncio.Event(), asyncio.Event()
    original = store._update_manifest

    async def delayed(entry, value):
        if value["source"] == "bundled@A":
            entered.set()
            await resume.wait()
        await original(entry, value)

    monkeypatch.setattr(store, "_update_manifest", delayed)
    first = asyncio.create_task(
        store.put_resource("prompt", "system", b"A", source="bundled@A")
    )
    await asyncio.wait_for(entered.wait(), 2)
    await store.put_resource("prompt", "system", b"B", source="bundled@B")
    resume.set()
    await first
    (resource,) = await store.list_resources()
    assert await store.read("prompt", "system") == b"B"
    assert resource.source == "bundled@B"
    assert resource.version == hashlib.sha256(b"B").hexdigest()


async def test_sweeper_fences_late_alias_publication(monkeypatch):
    now = datetime.now(UTC)
    kv = MemoryKV()
    store = KvThreadStore(kv, clock=lambda: now)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = kv.put
    pause = True

    async def delayed(name, data, *, if_version):
        nonlocal pause
        if pause and name == keys.head_key(KEY):
            pause = False
            entered.set()
            await resume.wait()
        return await original(name, data, if_version=if_version)

    monkeypatch.setattr(kv, "put", delayed)
    writing = asyncio.create_task(
        store.append_side(
            KEY, SideChange(op_id="alias", add_aliases=["shared"])
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    now += timedelta(hours=2)
    assert await _Sweeper(store, now - timedelta(hours=1)).aliases() == 1
    resume.set()
    await writing
    assert await store.resolve_alias("shared") == KEY


async def test_untrusted_conditional_delete_keeps_orphans(monkeypatch):
    now = datetime.now(UTC)
    kv = MemoryKV()
    kv.conditional_delete = False
    store = KvThreadStore(kv, clock=lambda: now)
    await store.append_side(
        KEY, SideChange(op_id="alias", add_aliases=["shared"])
    )
    await store.delete(KEY, op_id="deleted")
    assert await kv.get(keys.alias_key("shared")) is not None
    now += timedelta(hours=2)

    async def forbidden(*_args):
        raise AssertionError("unsafe conditional delete")

    monkeypatch.setattr(kv, "delete_if", forbidden)
    sweeper = _Sweeper(store, now - timedelta(hours=1))
    assert await sweeper.aliases() == 0
    assert await sweeper.chunks() == 0
