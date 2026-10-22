"""Запись итога хода в файле треда: место, ``seq``, откат, сжатие."""

import json

from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.thread_paths import sanitize_system_id
from gc_core.thread_store import (
    ThreadSession,
    append_new_thread_messages,
    append_turn_outcome_record,
    discard_message_by_id,
    load_thread_messages,
    load_thread_meta,
)
from gc_core.thread_store_async import SyncThreadStoreAdapter
from gc_core.thread_store_file import FileThreadStore
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_testkit.memory_thread_store import MemoryThreadStore
from gc_testkit.settings import make_settings


def _outcome(opened_by="u1"):
    return TurnOutcome(
        status="done",
        reason=None,
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

    assert await session.aappend_turn_outcome(_outcome(), discarded=False)

    rows = _live(tmp_path)
    assert _kinds(rows) == ["u1", "a1", "turn_outcome"]
    assert rows[2]["seq"] == 2
    assert rows[2]["thread_id"] == "t"
    assert rows[2]["system"] == "cli"
    assert rows[2]["outcome"] == _outcome().to_payload()
    assert isinstance(rows[2]["ts"], str)
    assert "payload" not in rows[2]
    assert session.persisted_messages_count == 2


async def test_loader_skips_record_and_next_message_gets_next_seq(tmp_path):
    session = await _session(FileThreadStore(str(tmp_path)))
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )
    await session.aappend_turn_outcome(_outcome(), discarded=False)

    messages, _, max_seq = load_thread_messages(str(tmp_path), "cli", "t")
    assert [m["id"] for m in messages] == ["u1", "a1"]
    assert max_seq == 2

    await session.apersist_batch([_user("u2")], previous_response_id=None)
    assert [row["seq"] for row in _live(tmp_path)] == [1, 2, 2, 3]


async def test_record_does_not_touch_messages_count(tmp_path):
    session = await _session(FileThreadStore(str(tmp_path)))
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )
    await session.aappend_turn_outcome(_outcome(), discarded=False)

    assert load_thread_meta(str(tmp_path), "cli", "t")["messages_count"] == 2


def test_envelope_system_is_sanitized_like_message_lines(tmp_path):
    append_new_thread_messages(
        str(tmp_path), "My CLI", "t", [_user("u1")], set()
    )
    append_turn_outcome_record(str(tmp_path), "My CLI", "t", _outcome(), seq=1)

    path = tmp_path / f"data_{sanitize_system_id('My CLI')}_t.jsonl"
    message, record = _rows(path)
    assert record["system"] == sanitize_system_id("My CLI")
    assert record["system"] == message["system"]


async def test_deferred_messages_are_written_before_record(tmp_path):
    # persist: turn сейчас включается только у durable-хранилища, а оно
    # записей не пишет. Флаг ставится руками: страж порядка самой операции.
    session = await _session(FileThreadStore(str(tmp_path)))
    session._defer_persist = True
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    await session.apersist_batch([_answer("a1")], previous_response_id=None)

    assert await session.aappend_turn_outcome(_outcome(), discarded=False)

    rows = _live(tmp_path)
    assert _kinds(rows) == ["u1", "a1", "turn_outcome"]
    assert rows[2]["seq"] == 2


async def test_discarded_turn_does_not_flush_deferred(tmp_path):
    session = await _session(FileThreadStore(str(tmp_path)))
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    session._defer_persist = True
    await session.apersist_batch([_answer("a1")], previous_response_id=None)

    assert await session.aappend_turn_outcome(_outcome(), discarded=True)

    rows = _live(tmp_path)
    assert _kinds(rows) == ["u1", "turn_outcome"]
    assert rows[1]["seq"] == 1


async def test_empty_thread_gets_no_record(tmp_path):
    session = await _session(FileThreadStore(str(tmp_path)))

    assert not await session.aappend_turn_outcome(_outcome(), discarded=False)
    assert not (tmp_path / "data_cli_t.jsonl").exists()


async def test_store_without_capability_gets_no_record():
    store = MemoryThreadStore()
    session = await _session(store)
    await session.apersist_batch([_user("u1")], previous_response_id=None)

    assert not await session.aappend_turn_outcome(_outcome(), discarded=False)
    assert [m["id"] for m in store.load_messages("cli", "t")] == ["u1"]


def test_adapter_exposes_capability_only_when_store_has_it(tmp_path):
    memory = SyncThreadStoreAdapter(MemoryThreadStore())
    assert memory.aappend_turn_outcome is None
    durable = DurableFileThreadStore(str(tmp_path))
    assert SyncThreadStoreAdapter(durable).aappend_turn_outcome is None
    plain = SyncThreadStoreAdapter(FileThreadStore(str(tmp_path)))
    assert callable(plain.aappend_turn_outcome)


async def test_durable_store_writes_no_record_and_recovers(tmp_path):
    store = DurableFileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )
    before = (tmp_path / "data_cli_t.jsonl").read_bytes()

    assert not await session.aappend_turn_outcome(_outcome(), discarded=False)

    assert (tmp_path / "data_cli_t.jsonl").read_bytes() == before
    await session.apersist_batch([_user("u2")], previous_response_id=None)
    snapshot = DurableFileThreadStore(str(tmp_path)).read_snapshot("cli", "t")
    assert [m["id"] for m in snapshot.state.messages] == ["u1", "a1", "u2"]
    assert snapshot.state.max_seq == 3


async def _two_turns(tmp_path):
    """Ход ``u0`` с записью, затем ход ``u1`` с ответом."""
    session = await _session(FileThreadStore(str(tmp_path)))
    await session.apersist_batch(
        [_user("u0"), _answer("a0")], previous_response_id=None
    )
    await session.aappend_turn_outcome(_outcome("u0"), discarded=False)
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )
    return session


async def test_discard_removes_reply_and_its_record_only(tmp_path):
    session = await _two_turns(tmp_path)
    await session.aappend_turn_outcome(_outcome("u1"), discarded=False)

    assert discard_message_by_id(str(tmp_path), "cli", "t", "u1")

    rows = _live(tmp_path)
    assert _kinds(rows) == ["u0", "a0", "turn_outcome", "a1"]
    assert rows[2]["outcome"]["opened_by"] == "u0"
    assert load_thread_meta(str(tmp_path), "cli", "t")["messages_count"] == 3


async def test_second_discard_removes_record_written_after_first(tmp_path):
    await _two_turns(tmp_path)
    assert discard_message_by_id(str(tmp_path), "cli", "t", "u1")
    append_turn_outcome_record(
        str(tmp_path), "cli", "t", _outcome("u1"), seq=3
    )

    assert discard_message_by_id(str(tmp_path), "cli", "t", "u1")

    assert _kinds(_live(tmp_path)) == ["u0", "a0", "turn_outcome", "a1"]
    assert load_thread_meta(str(tmp_path), "cli", "t")["messages_count"] == 3
    assert not discard_message_by_id(str(tmp_path), "cli", "t", "u1")


async def test_record_without_opener_survives_discard(tmp_path):
    session = await _session(FileThreadStore(str(tmp_path)))
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )
    await session.aappend_turn_outcome(_outcome(None), discarded=False)

    assert discard_message_by_id(str(tmp_path), "cli", "t", "u1")

    assert _kinds(_live(tmp_path)) == ["a1", "turn_outcome"]


def _summary_payload():
    return {"summary_message_content": "сводка", "dialog_steps_offset": 1}


async def test_turn_end_compaction_archives_record_with_turn(tmp_path):
    session = await _session(FileThreadStore(str(tmp_path)))
    await session.apersist_batch(
        [_user("u1"), _answer("a1")], previous_response_id=None
    )
    await session.aappend_turn_outcome(_outcome(), discarded=False)

    result = await session.aarchive_and_compact_summary_payload(
        _summary_payload()
    )

    (archive,) = tmp_path.glob("data_cli_t__pre_summary__*.jsonl")
    assert _kinds(_rows(archive)) == ["u1", "a1", "turn_outcome"]
    live = _live(tmp_path)
    assert len(live) == 1
    assert live[0]["payload"]["id"] == result["newly_summarized_message_id"]


async def test_mid_turn_compaction_record_follows_turn_messages(tmp_path):
    session = await _session(FileThreadStore(str(tmp_path)))
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    result = await session.aarchive_and_compact_summary_payload(
        _summary_payload()
    )
    await session.apersist_batch([_answer("a1")], previous_response_id=None)

    assert await session.aappend_turn_outcome(_outcome(), discarded=False)

    summary_id = result["newly_summarized_message_id"]
    rows = _live(tmp_path)
    assert _kinds(rows) == [summary_id, "a1", "turn_outcome"]
    assert rows[2]["seq"] == 2
    assert load_thread_meta(str(tmp_path), "cli", "t")["messages_count"] == 2
