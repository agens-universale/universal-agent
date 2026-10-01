from dataclasses import replace

import pytest
from gc_core.execution.contracts import ExecutionFailure, ProjectionBatch
from gc_testkit.thread_store_conformance import assert_conforms
from gc_ua_temporalio.storage.artifacts import ArtifactStore
from gc_ua_temporalio.storage.sqlite import (
    ProjectionConflict,
    SQLiteThreadStore,
)


def test_sqlite_passes_existing_store_contract(tmp_path):
    counter = iter(range(100))
    assert_conforms(
        lambda: SQLiteThreadStore(tmp_path / f"store-{next(counter)}.sqlite")
    )


def batch(store):
    epoch = store.claim("sys", "thread", "owner")
    return ProjectionBatch(
        "sys",
        "thread",
        "owner",
        epoch,
        "event-1",
        1,
        [{"id": "m1", "role": "user", "content": "hello"}],
        {"shared": {"working_memory": {"goal": "ship"}}},
    )


def test_lost_ack_repeat_and_stale_writer(tmp_path):
    path = tmp_path / "s.sqlite"
    store = SQLiteThreadStore(path)
    value = batch(store)
    first = store.commit_batch(value)
    # Reopen the database: there is no in-memory dedup cache.
    restarted = SQLiteThreadStore(path)
    assert restarted.commit_batch(value) == first
    assert len(restarted.load_messages("sys", "thread")) == 1
    assert (
        restarted.load_meta("sys", "thread")["shared"]["working_memory"][
            "goal"
        ]
        == "ship"
    )
    for changed in (
        replace(value, meta={"lost": True}),
        replace(value, owner="other"),
        replace(value, event_id="late", sequence=0),
        replace(value, epoch=value.epoch - 1),
    ):
        with pytest.raises(ProjectionConflict):
            restarted.commit_batch(changed)
    for write in (
        lambda: restarted.save_meta("sys", "thread", {}),
        lambda: restarted.append_messages("sys", "thread", []),
        lambda: restarted.reset("sys", "thread"),
    ):
        with pytest.raises(ProjectionConflict):
            write()


def test_crash_between_messages_meta_ack_rolls_back(tmp_path):
    class Broken(SQLiteThreadStore):
        def _after_messages(self, db):
            raise OSError("power lost")

    path = tmp_path / "s.sqlite"
    broken = Broken(path)
    value = batch(broken)
    with pytest.raises(OSError):
        broken.commit_batch(value)
    restarted = SQLiteThreadStore(path)
    assert restarted.load_messages("sys", "thread") == []
    assert restarted.load_meta("sys", "thread") == {}
    ack = restarted.commit_batch(value)
    assert ack.written == 1
    assert (
        restarted.commit_batch(
            replace(value, event_id="event-2", sequence=2)
        ).written
        == 0
    )


def test_artifact_immutable_and_verified(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = store.put("Пример".encode())
    assert store.put("Пример".encode()) == ref
    assert store.read(ref).decode() == "Пример"
    (tmp_path / ref.digest).write_bytes(b"broken")
    with pytest.raises(ExecutionFailure):
        store.read(ref)
    with pytest.raises(ValueError):
        store.read(replace(ref, digest="../escape"))


async def test_offline_import_is_repeatable_and_blocks_local_reopen(tmp_path):
    from gc_core.thread_store import ThreadSession
    from gc_testkit.memory_thread_store import MemoryThreadStore
    from gc_ua_temporalio.storage.import_thread import import_stopped_thread

    source = MemoryThreadStore()
    source.append_messages(
        "sys", "thread", [{"id": "u", "role": "user", "content": "original"}]
    )
    source.save_meta(
        "sys", "thread", {"shared": {"working_memory": {"goal": "continue"}}}
    )
    target = SQLiteThreadStore(tmp_path / "target.sqlite")
    with pytest.raises(ExecutionFailure):
        await import_stopped_thread(source, target, "sys", "thread")
    first = await import_stopped_thread(
        source, target, "sys", "thread", writers_stopped=True
    )
    assert (
        await import_stopped_thread(
            source, target, "sys", "thread", writers_stopped=True
        )
        == first
    )
    assert target.load_messages("sys", "thread")[0]["content"] == "original"
    with pytest.raises(ExecutionFailure):
        await ThreadSession.afrom_config(
            config_dict={}, system_id="sys", thread_id="thread", store=source
        )
