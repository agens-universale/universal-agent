"""Фасад Threads: хвост с курсором, состояние фронтенда, части истории,
обход тредов и синхронный мост для TUI."""

import asyncio

import pytest
from gc_core.threads.facade import Threads, run_sync
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import RuntimeStatus, ThreadKey
from gc_testkit.legacy_threads import LEGACY_THREADS, install_legacy_threads
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore

KEY = ThreadKey(system="cli", thread="t1")


def _msgs(*ids):
    return [{"id": i, "role": "user", "content": f"текст {i}"} for i in ids]


@pytest.fixture(params=["memory", "file"])
def threads(request, tmp_path):
    if request.param == "memory":
        return Threads(MemoryThreadStore(), owner="host")
    return Threads(FileThreadStore(tmp_path), owner="host")


def test_tail_from_now_then_new_entries(threads):
    async def scenario():
        await threads.append_messages(KEY, _msgs("a", "b"))
        start = await threads.tail(KEY, ["messages"], None)
        assert start.entries == [] and start.cursor.seq["messages"] == 2
        await threads.append_messages(KEY, _msgs("c"))
        batch = await threads.tail(KEY, ["messages"], start.cursor)
        assert [e.data["id"] for _s, e in batch.entries] == ["c"]
        assert not batch.reset
        idle = await threads.tail(KEY, ["messages"], batch.cursor)
        assert idle.entries == []

    asyncio.run(scenario())


def test_tail_resets_after_compaction_and_recreation(threads):
    async def scenario():
        await threads.append_messages(KEY, _msgs("a", "b"))
        cursor = (await threads.tail(KEY, ["messages"], None)).cursor
        port = threads._port
        await port.aarchive_and_replace("cli", "t1", _msgs("summary"))
        batch = await threads.tail(KEY, ["messages"], cursor)
        assert batch.reset
        assert [e.data["id"] for _s, e in batch.entries] == ["summary"]
        await threads.delete(KEY)
        await threads.append_messages(KEY, _msgs("n1"))
        again = await threads.tail(KEY, ["messages"], batch.cursor)
        assert again.reset
        assert [e.data["id"] for _s, e in again.entries] == ["n1"]

    asyncio.run(scenario())


def test_frontend_patches_merge_and_survive_agent_step(threads):
    async def scenario():
        await threads.update_frontend(KEY, "tui", {"status": "running"})
        await threads.update_frontend(KEY, "tui", {"branch": "main"})
        port = threads._port
        meta = await port.aload_meta("cli", "t1")
        await port.apersist_step("cli", "t1", _msgs("a"), meta)
        record = await threads.get(KEY)
        assert record.frontend["tui"] == {
            "status": "running",
            "branch": "main",
        }

    asyncio.run(scenario())


def test_discard_updates_runtime_when_given(threads):
    async def scenario():
        await threads.append_messages(KEY, _msgs("a", "b"))
        runtime = RuntimeStatus(live_msg_count=1, live_history_chars=7)
        assert await threads.discard_message(KEY, "a", runtime=runtime)
        record = await threads.get(KEY)
        assert record.runtime.live_msg_count == 1
        assert [e.data["id"] for e in await threads.history(KEY)] == ["b"]
        assert not await threads.discard_message(KEY, "нет такого")

    asyncio.run(scenario())


def test_iter_threads_filters_by_system(threads):
    async def scenario():
        for system, thread in (("cli", "a"), ("cli", "b"), ("tui", "c")):
            key = ThreadKey(system=system, thread=thread)
            await threads.append_messages(key, _msgs(f"{thread}1"))
        found = [
            r.key.thread async for r in threads.iter_threads(system="cli")
        ]
        assert sorted(found) == ["a", "b"]

    asyncio.run(scenario())


def test_parts_of_compacted_develop_thread(tmp_path):
    fixture = LEGACY_THREADS["cli_compacted"]
    threads = Threads(FileThreadStore(install_legacy_threads(tmp_path)))
    key = ThreadKey(system=fixture.system, thread=fixture.thread_id)
    parts = asyncio.run(threads.parts(key))
    assert len(parts) == fixture.archives + 1
    assert len(parts[-1]) == fixture.messages


def test_run_sync_drives_local_store(tmp_path):
    threads = Threads(FileThreadStore(tmp_path), owner="host")
    assert run_sync(threads.append_messages(KEY, _msgs("a"))) == 1
    assert run_sync(threads.get(KEY)).info.messages_count == 1


def test_run_sync_refuses_store_that_waits():
    class Waiting(MemoryThreadStore):
        async def get_thread(self, key):
            await asyncio.sleep(0)
            return await super().get_thread(key)

    with pytest.raises(RuntimeError, match="локального"):
        run_sync(Threads(Waiting()).get(KEY))
