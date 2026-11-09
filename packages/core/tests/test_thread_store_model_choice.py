"""Смена модели — поток ``model_choices`` хранилища v2: привязка к
последнему записанному сообщению, запись в ходе и между ходами."""

import pytest
from gc_core.persistence.model_choice_record import ModelChoiceRecord
from gc_core.thread_store import ThreadSession
from gc_core.threads.errors import ThreadBusy
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import LeaseRequest, ThreadKey
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="cli", thread="t")
RECORD = ModelChoiceRecord(choice="max", alias="big", before="light")


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


async def _choices(store):
    return [e.data for e in await store.read_stream(KEY, "model_choices")]


async def _ids(store):
    return [e.data["id"] for e in await store.read_stream(KEY, "messages")]


async def test_record_is_anchored_to_last_written_message(tmp_path):
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )

    assert await session.aappend_model_choice(RECORD, discarded=False)

    assert await _choices(store) == [
        {"after": "a1", "model": RECORD.to_payload()}
    ]
    record = await store.get_thread(KEY)
    assert record.max_seq["messages"] == 2
    assert record.info.messages_count == 2
    assert await _ids(store) == ["u1", "a1"]


async def test_deferred_messages_are_written_before_record(tmp_path):
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    session._defer_persist = True
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    await session.apersist_batch([_answer("a1")], previous_response_id=None)

    assert await session.aappend_model_choice(RECORD, discarded=False)

    assert await _ids(store) == ["u1", "a1"]
    assert [c["after"] for c in await _choices(store)] == ["a1"]


async def test_discarded_turn_does_not_flush_deferred(tmp_path):
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    session._defer_persist = True
    await session.apersist_batch([_answer("a1")], previous_response_id=None)

    assert await session.aappend_model_choice(RECORD, discarded=True)

    assert await _ids(store) == ["u1"]
    assert [c["after"] for c in await _choices(store)] == ["u1"]


async def test_empty_thread_gets_no_record(tmp_path):
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)

    assert not await session.aappend_model_choice(RECORD, discarded=False)
    assert await store.get_thread(KEY) is None


async def test_any_store_records_the_change():
    store = MemoryThreadStore()
    session = await _session(store)
    await session.apersist_batch([_user("u1")], previous_response_id=None)

    assert await session.aappend_model_choice(RECORD, discarded=False)

    assert [c["after"] for c in await _choices(store)] == ["u1"]


async def test_between_turns_record_follows_last_message(tmp_path):
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )
    await session.arelease_lease()

    assert await Threads(store).append_model_choice(KEY, RECORD)

    assert await _choices(store) == [
        {"after": "a1", "model": RECORD.to_payload()}
    ]
    assert await _ids(store) == ["u1", "a1"]


async def test_between_turns_without_thread_writes_nothing(tmp_path):
    store = FileThreadStore(str(tmp_path))

    assert not await Threads(store).append_model_choice(KEY, RECORD)

    assert await store.get_thread(KEY) is None


async def test_between_turns_on_busy_thread_is_refused():
    """Тред держит ход другого владельца: запись между ходами отказана
    (``ThreadBusy``), поток не тронут."""
    store = MemoryThreadStore()
    session = await _session(store)
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    await session.arelease_lease()
    await store.claim(KEY, LeaseRequest(owner="workflow", ttl=None))

    with pytest.raises(ThreadBusy):
        await Threads(store).append_model_choice(KEY, RECORD)
    assert await _choices(store) == []
