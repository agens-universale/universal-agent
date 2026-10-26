"""Запись смены модели в файле треда: место, ``seq``, хранилища, сжатие,
запись между ходами."""

import json

from gc_core.persistence.model_choice_record import (
    ModelChoiceRecord,
    parse_model_choice,
)
from gc_core.thread_store import (
    ThreadSession,
    append_model_choice_after_last_message,
    load_thread_messages,
    load_thread_meta,
)
from gc_core.thread_store_async import SyncThreadStoreAdapter
from gc_core.thread_store_file import FileThreadStore
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_testkit.memory_thread_store import MemoryThreadStore
from gc_testkit.settings import make_settings

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


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _live(tmp_path):
    return _rows(tmp_path / "data_cli_t.jsonl")


def _kinds(rows):
    return [
        row["payload"]["id"] if row["type"] == "message" else row["type"]
        for row in rows
    ]


async def test_record_follows_last_message_with_its_seq(tmp_path):
    session = await _session(FileThreadStore(str(tmp_path)))
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )

    assert await session.aappend_model_choice(RECORD, discarded=False)

    rows = _live(tmp_path)
    assert _kinds(rows) == ["u1", "a1", "model_choice"]
    assert rows[2]["seq"] == 2
    assert rows[2]["thread_id"] == "t"
    assert rows[2]["system"] == "cli"
    assert "payload" not in rows[2]
    assert parse_model_choice(rows[2]) == RECORD
    assert session.persisted_messages_count == 2


async def test_record_is_not_a_message(tmp_path):
    session = await _session(FileThreadStore(str(tmp_path)))
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )
    await session.aappend_model_choice(RECORD, discarded=False)

    messages, _, max_seq = load_thread_messages(str(tmp_path), "cli", "t")
    assert [m["id"] for m in messages] == ["u1", "a1"]
    assert max_seq == 2
    assert load_thread_meta(str(tmp_path), "cli", "t")["messages_count"] == 2
    await session.apersist_batch([_user("u2")], previous_response_id=None)
    assert [row["seq"] for row in _live(tmp_path)] == [1, 2, 2, 3]


async def test_empty_thread_gets_no_record(tmp_path):
    session = await _session(FileThreadStore(str(tmp_path)))

    assert not await session.aappend_model_choice(RECORD, discarded=False)
    assert not (tmp_path / "data_cli_t.jsonl").exists()


async def test_store_without_capability_gets_no_record():
    store = MemoryThreadStore()
    session = await _session(store)
    await session.apersist_batch([_user("u1")], previous_response_id=None)

    assert not await session.aappend_model_choice(RECORD, discarded=False)
    assert [m["id"] for m in store.load_messages("cli", "t")] == ["u1"]


def test_adapter_exposes_capability_only_when_store_has_it(tmp_path):
    memory = SyncThreadStoreAdapter(MemoryThreadStore())
    assert memory.aappend_model_choice is None
    durable = DurableFileThreadStore(str(tmp_path))
    assert SyncThreadStoreAdapter(durable).aappend_model_choice is None
    plain = SyncThreadStoreAdapter(FileThreadStore(str(tmp_path)))
    assert callable(plain.aappend_model_choice)


async def test_durable_store_writes_no_record_and_recovers(tmp_path):
    store = DurableFileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )
    before = (tmp_path / "data_cli_t.jsonl").read_bytes()

    assert not await session.aappend_model_choice(RECORD, discarded=False)

    assert (tmp_path / "data_cli_t.jsonl").read_bytes() == before
    await session.apersist_batch([_user("u2")], previous_response_id=None)
    snapshot = DurableFileThreadStore(str(tmp_path)).read_snapshot("cli", "t")
    assert [m["id"] for m in snapshot.state.messages] == ["u1", "a1", "u2"]


async def test_discarded_turn_does_not_flush_deferred(tmp_path):
    session = await _session(FileThreadStore(str(tmp_path)))
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    session._defer_persist = True
    await session.apersist_batch([_answer("a1")], previous_response_id=None)

    assert await session.aappend_model_choice(RECORD, discarded=True)

    rows = _live(tmp_path)
    assert _kinds(rows) == ["u1", "model_choice"]
    assert rows[1]["seq"] == 1


async def test_turn_end_compaction_archives_record_with_turn(tmp_path):
    session = await _session(FileThreadStore(str(tmp_path)))
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )
    await session.aappend_model_choice(RECORD, discarded=False)

    await session.aarchive_and_compact_summary_payload(
        {"summary_message_content": "сводка", "dialog_steps_offset": 1}
    )

    (archive,) = tmp_path.glob("data_cli_t__pre_summary__*.jsonl")
    assert _kinds(_rows(archive)) == ["u1", "a1", "model_choice"]
    assert [row["type"] for row in _live(tmp_path)] == ["message"]


async def test_between_turns_record_follows_last_message(tmp_path):
    session = await _session(FileThreadStore(str(tmp_path)))
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )

    assert append_model_choice_after_last_message(
        str(tmp_path), "cli", "t", RECORD
    )

    rows = _live(tmp_path)
    assert _kinds(rows) == ["u1", "a1", "model_choice"]
    assert rows[2]["seq"] == 2
    assert parse_model_choice(rows[2]) == RECORD


def test_between_turns_without_thread_writes_nothing(tmp_path):
    assert not append_model_choice_after_last_message(
        str(tmp_path), "cli", "t", RECORD
    )
    assert not (tmp_path / "data_cli_t.jsonl").exists()
