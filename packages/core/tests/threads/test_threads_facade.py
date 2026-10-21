"""Фасад Threads: хвост с курсором, состояние фронтенда, части истории,
обход тредов и синхронный мост для TUI."""

import asyncio

import pytest
from gc_core.threads.facade import Threads, run_sync
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import (
    Compaction,
    RuntimeStatus,
    SessionState,
    SideChange,
    ThreadKey,
)
from gc_core.threads.session_writer import SessionWriter
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


def test_snapshot_reads_everything_and_continues_with_tail(threads):
    async def scenario():
        assert await threads.snapshot(KEY, ["messages"]) is None
        await threads.append_messages(KEY, _msgs("a", "b"))
        start = await threads.snapshot(KEY, ["messages"])
        assert [e.data["id"] for _s, e in start.entries] == ["a", "b"]
        assert not start.reset
        await threads.append_messages(KEY, _msgs("c"))
        batch = await threads.tail(KEY, ["messages"], start.cursor)
        assert [e.data["id"] for _s, e in batch.entries] == ["c"]

    asyncio.run(scenario())


def test_tail_resets_after_compaction_and_recreation(threads):
    async def scenario():
        await threads.append_messages(KEY, _msgs("a", "b"))
        cursor = (await threads.tail(KEY, ["messages"], None)).cursor
        writer = SessionWriter(threads.store, KEY, owner="compactor")
        await writer.aclaim()
        await writer.acommit(
            replace_history=Compaction(messages=_msgs("summary"))
        )
        await writer.arelease()
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
        # Шаг хода: сессия пишет историю и своё состояние, фронтенд не трогает.
        writer = SessionWriter(threads.store, KEY, owner="agent")
        await writer.aclaim()
        await writer.acommit(
            append_messages=_msgs("a"), set_state=SessionState(notes="шаг")
        )
        await writer.arelease()
        record = await threads.get(KEY)
        assert record.frontend["tui"] == {
            "status": "running",
            "branch": "main",
        }

    asyncio.run(scenario())


def test_frontend_stream_alias_and_summary_in_one_write(threads):
    async def scenario():
        await threads.append_frontend(
            KEY,
            "chat.responses",
            [{"response_id": "resp_1"}],
            aliases=["resp_1"],
            patch=("chat", {"latest_response_id": "resp_1"}),
        )
        assert await threads.resolve("resp_1") == KEY
        entries = await threads.read_frontend(KEY, "chat.responses")
        assert [e.data for e in entries] == [{"response_id": "resp_1"}]
        record = await threads.get(KEY)
        assert record.frontend["chat"] == {"latest_response_id": "resp_1"}

    asyncio.run(scenario())


def test_discard_matches_by_id_not_by_text(threads):
    """Откат осиротевшей реплики (быстрая отмена): удаляется запись с этим
    ``id``, повтор того же текста и вывод тула целы."""

    async def scenario():
        same = "одинаковый"
        await threads.append_messages(
            KEY,
            [
                {"role": "user", "content": same, "id": "m_orphan"},
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "x",
                    "arguments": "{}",
                    "id": "a1",
                },
                {"role": "user", "content": same, "id": "m_resend"},
            ],
        )
        assert await threads.discard_message(KEY, "m_orphan")
        ids = [e.data["id"] for e in await threads.history(KEY)]
        assert ids == ["a1", "m_resend"]
        assert (await threads.get(KEY)).info.messages_count == 2

    asyncio.run(scenario())


def test_discard_recounts_live_volume_and_keeps_the_rest(threads):
    async def scenario():
        await threads.append_messages(KEY, _msgs("a", "b"))
        await threads.store.append_side(
            KEY,
            SideChange(op_id="img", set_runtime=RuntimeStatus(image_seq=3)),
        )

        def recount(messages):
            return {"live_msg_count": len(messages), "live_history_chars": 7}

        assert await threads.discard_message(KEY, "a", recount=recount)
        runtime = (await threads.get(KEY)).runtime
        assert (runtime.live_msg_count, runtime.live_history_chars) == (1, 7)
        assert runtime.image_seq == 3
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
