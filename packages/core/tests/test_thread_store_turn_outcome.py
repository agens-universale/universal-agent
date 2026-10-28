"""Итог хода — поток ``turns`` хранилища v2: привязка, runtime, сироты."""

import pytest
from gc_core.persistence.turn_outcome import TurnOutcome, turn_shown_in
from gc_core.thread_store import ThreadSession
from gc_core.threads.errors import ThreadBusy
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import LeaseRequest, ThreadKey
from gc_core.threads.session_writer import SessionWriter
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="cli", thread="t")


def _outcome(opened_by="u1", status="done", reason=None):
    return TurnOutcome(
        status=status,
        reason=reason,
        opened_by=opened_by,
        started_at="2026-10-22T11:30:53.004Z",
        ended_at="2026-10-22T11:32:05.118Z",
        duration_sec=63.4,
        error=None,
    )


def _user(mid):
    return {"id": mid, "role": "user", "content": f"вопрос {mid}"}


def _answer(mid):
    return {"id": mid, "role": "assistant", "content": f"ответ {mid}"}


async def _session(store):
    return await ThreadSession.afrom_config(
        settings=make_settings({}),
        system_id="cli",
        thread_id="t",
        store=store,
    )


async def _turns(store):
    return [e.data for e in await store.read_stream(KEY, "turns")]


async def _ids(store):
    return {e.data["id"] for e in await store.read_stream(KEY, "messages")}


async def test_record_is_anchored_to_last_message_of_the_turn(tmp_path):
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )

    assert await session.aappend_turn_outcome(_outcome(), discarded=False)

    assert await _turns(store) == [
        {"after": "a1", "outcome": _outcome().to_payload()}
    ]
    record = await store.get_thread(KEY)
    assert record.max_seq["messages"] == 2
    assert record.info.messages_count == 2


async def test_anchor_of_message_without_id_is_the_id_readers_see(tmp_path):
    """Ответ модели без ``id``: хранилище даёт ему id на чтении — запись
    привязана к нему, а не пропускает привязку."""
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    answer = {"type": "message", "role": "assistant", "content": "ответ"}
    await session.apersist_batch(
        [_user("u1"), answer], previous_response_id=None
    )

    await session.aappend_turn_outcome(_outcome(), discarded=False)

    last = (await store.read_stream(KEY, "messages"))[-1].data["id"]
    assert [t["after"] for t in await _turns(store)] == [last]


async def test_record_sets_last_turn_in_runtime(tmp_path):
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch([_user("u1")], previous_response_id=None)

    await session.aappend_turn_outcome(
        _outcome(status="stopped", reason="step_limit"), discarded=False
    )

    runtime = (await store.get_thread(KEY)).runtime
    assert runtime.last_turn == {"status": "stopped", "reason": "step_limit"}
    assert runtime.live_msg_count == 1
    assert session.runtime == runtime


async def test_deferred_messages_are_written_before_record(tmp_path):
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    session._defer_persist = True
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    await session.apersist_batch([_answer("a1")], previous_response_id=None)

    assert await session.aappend_turn_outcome(_outcome(), discarded=False)

    assert await _ids(store) == {"u1", "a1"}
    assert [t["after"] for t in await _turns(store)] == ["a1"]


async def test_discarded_turn_does_not_flush_deferred(tmp_path):
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    session._defer_persist = True
    await session.apersist_batch([_answer("a1")], previous_response_id=None)

    assert await session.aappend_turn_outcome(_outcome(), discarded=True)

    assert await _ids(store) == {"u1"}
    assert [t["after"] for t in await _turns(store)] == ["u1"]


async def test_empty_thread_gets_no_record(tmp_path):
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)

    assert not await session.aappend_turn_outcome(_outcome(), discarded=False)
    assert await store.get_thread(KEY) is None


async def test_any_store_records_the_turn():
    store = MemoryThreadStore()
    session = await _session(store)
    await session.apersist_batch([_user("u1")], previous_response_id=None)

    assert await session.aappend_turn_outcome(_outcome(), discarded=False)

    assert [t["after"] for t in await _turns(store)] == ["u1"]


async def test_durable_owner_records_the_turn_by_its_lease():
    """Тред с бессрочной арендой (durable-исполнитель): запись хода сбоку
    проходит только по ней — итог хода пишется."""
    store = MemoryThreadStore()
    request = LeaseRequest(owner="workflow", ttl=None)
    lease = (await store.claim(KEY, request)).lease
    session = await _session(store)
    session.writer = SessionWriter(
        store, KEY, owner="workflow", ttl=None, lease=lease
    )
    await session.apersist_batch([_user("u1")], previous_response_id=None)

    assert await session.aappend_turn_outcome(_outcome(), discarded=False)

    assert [t["after"] for t in await _turns(store)] == ["u1"]


async def test_foreign_durable_owner_refuses_the_record():
    """Тред ведёт durable-исполнитель, своей аренды у сессии нет: запись
    отказана (``OwnershipError`` → ``ThreadBusy``), поток не тронут."""
    store = MemoryThreadStore()
    session = await _session(store)
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    await session.arelease_lease()
    await store.claim(KEY, LeaseRequest(owner="workflow", ttl=None))
    session.writer._released = False

    with pytest.raises(ThreadBusy):
        await session.aappend_turn_outcome(_outcome(), discarded=False)
    assert await _turns(store) == []


async def test_released_thread_gets_no_late_record(tmp_path):
    """Тред отпущен: его может вести другой ход — поздняя запись итога
    затёрла бы его runtime."""
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    await session.arelease_lease()

    assert not await session.aappend_turn_outcome(_outcome(), discarded=False)
    assert await _turns(store) == []


def _summary_payload():
    return {"summary_message_content": "сводка", "dialog_steps_offset": 1}


async def test_compaction_keeps_record_visible_by_its_reply(tmp_path):
    """Сжатие конца хода уносит сообщения хода в архив, поток итогов не
    трогает: реплика хода — в истории треда (архив), запись видна."""
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )
    await session.aappend_turn_outcome(_outcome(), discarded=False)

    await session.aarchive_and_compact_summary_payload(_summary_payload())

    (record,) = await _turns(store)
    archive = (await store.get_thread(KEY)).archives[0].id
    archived = {e.data["id"] for e in await store.read_archive(KEY, archive)}
    assert turn_shown_in(record, archived | await _ids(store))


async def test_undone_reply_leaves_an_orphan_record(tmp_path):
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    await session.aappend_turn_outcome(_outcome("u1"), discarded=True)
    await session.arelease_lease()

    assert await Threads(store).discard_message(KEY, "u1")

    (record,) = await _turns(store)
    assert not turn_shown_in(record, await _ids(store))


async def test_reset_history_orphans_old_records(tmp_path):
    """Сброс треда (``-n``): поток итогов остаётся, его записи — сироты:
    у сообщений нового разговора другие id."""
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    await session.aappend_turn_outcome(_outcome("u1"), discarded=False)
    await session.writer.acommit(reset_history=True)
    fresh = await _session(store)
    await fresh.apersist_batch([_user("u2")], previous_response_id=None)

    (record,) = await _turns(store)
    assert not turn_shown_in(record, await _ids(store))
