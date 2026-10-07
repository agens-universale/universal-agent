import json
import sqlite3
import time
from dataclasses import replace

import pytest
from gc_core.thread_store import ThreadSession
from gc_core.thread_store_durable import (
    ThreadStoreConflict,
    ThreadStoreOperation,
    ThreadStoreOperationMismatch,
    ThreadStoreOwnershipError,
)
from gc_core_temporalio.storage.sqlite import SQLiteThreadStore


def operation(identity="append", kind="append_messages", **payload):
    return ThreadStoreOperation(identity, kind, payload)


def test_original_receipt_survives_restart_and_reset(tmp_path):
    path = tmp_path / "store.sqlite"
    store = SQLiteThreadStore(path)
    request = operation(messages=[{"id": "m1"}])
    first = store.commit_operation("s", "t", request)
    assert first.written == 1
    assert SQLiteThreadStore(path).commit_operation("s", "t", request) == first
    assert (
        store.commit_operation(
            "s", "t", operation("next", messages=[{"id": "m1"}])
        ).written
        == 0
    )
    with pytest.raises(ThreadStoreOperationMismatch):
        store.commit_operation(
            "s", "t", replace(request, payload={"messages": []})
        )
    reset = operation("reset", "reset")
    reset_receipt = store.commit_operation("s", "t", reset)
    store.append_messages("s", "t", [{"id": "new"}])
    assert store.commit_operation("s", "t", reset) == reset_receipt
    assert store.commit_operation("s", "t", request) == first
    assert store.load_messages("s", "t") == [{"id": "new"}]
    with pytest.raises(ThreadStoreOwnershipError):
        store.commit_operation("s", "t", replace(request, operation_id="late"))


def test_ownership_revision_and_original_ack(tmp_path):
    store = SQLiteThreadStore(tmp_path / "s.sqlite")
    append = operation(messages=[{"id": "first"}])
    ack = store.commit_operation("s", "t", append)
    claim = store.commit_operation(
        "s", "t", operation("claim", "claim_owner", owner_id="workflow")
    )
    assert store.commit_operation("s", "t", append) == ack
    owned = ThreadStoreOperation(
        "owned",
        "persist",
        {"messages": [{"id": "next"}], "meta": {}},
        owner_id="workflow",
        owner_epoch=claim.owner_epoch,
        expected_revision=claim.revision,
    )
    wrong_revision = replace(owned, expected_revision=0)
    with pytest.raises(ThreadStoreConflict):
        store.commit_operation("s", "t", wrong_revision)
    assert store.read_snapshot("s", "t").revision == claim.revision
    store.commit_operation("s", "t", owned)
    with pytest.raises(ThreadStoreOwnershipError):
        store.commit_operation(
            "s", "t", replace(owned, operation_id="wrong", owner_epoch=0)
        )


def test_persist_rollback_includes_meta_receipt_and_revision(tmp_path):
    class Broken(SQLiteThreadStore):
        def _after_messages(self, _db):
            raise OSError("crash before metadata")

    path = tmp_path / "s.sqlite"
    request = operation(
        "persist",
        "persist",
        messages=[{"id": "m1"}],
        meta={"shared": {"x": 1}},
        expected_written=1,
    )
    with pytest.raises(OSError):
        Broken(path).commit_operation("s", "t", request)
    restarted = SQLiteThreadStore(path)
    snapshot = restarted.read_snapshot("s", "t")
    assert (
        snapshot.state.messages == []
        and snapshot.meta == {}
        and snapshot.revision == 0
    )
    assert restarted.commit_operation("s", "t", request).written == 1


def test_compaction_archive_and_sidecar_receipts(tmp_path):
    path = tmp_path / "s.sqlite"
    store = SQLiteThreadStore(path)
    store.append_messages("s", "t", [{"id": "before"}])
    trace = operation(
        "trace",
        "append_subagent_trace",
        parent_call_id="p",
        child_run_id="c",
        payload={},
    )
    trace.payload["kind"] = "event"
    trace_ack = store.commit_operation("s", "t", trace)
    assert store.commit_operation("s", "t", trace) == trace_ack
    compact = operation(
        "compact",
        "compact",
        messages=[{"id": "summary"}],
        meta={"custom": True},
        archive_suffix="fixed",
    )
    receipt = store.commit_operation("s", "t", compact)
    assert (
        SQLiteThreadStore(path).commit_operation("s", "t", compact) == receipt
    )
    assert store.read_subagent_trace("s", "t") == [trace_ack.result]
    with sqlite3.connect(path) as db:
        assert json.loads(
            db.execute(
                "SELECT messages FROM thread_archives WHERE suffix='fixed'"
            ).fetchone()[0]
        ) == [{"id": "before"}]
    store.reset("s", "t")
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT count(*) FROM thread_archives").fetchone()[0]
            == 1
        )


async def test_real_thread_session_uses_sqlite_capability(tmp_path):
    store = SQLiteThreadStore(tmp_path / "s.sqlite")
    session = await ThreadSession.afrom_config(
        settings=None, system_id="s", thread_id="t", store=store
    )
    await session.apersist_batch(
        [{"id": "m1", "role": "user", "content": "hello"}],
        previous_response_id="response",
    )
    assert session.meta["messages_count"] == 1
    assert session.meta["runtime"]["live_msg_count"] == 1
    await session.aarchive_and_compact_summary_payload(
        {"summary_message_content": "summary"}
    )
    assert store.read_snapshot("s", "t").state.max_seq == 1
    assert len(store.load_messages("s", "t")) == 1


def test_local_mutations_invalidate_snapshot_and_patch_input(tmp_path):
    store = SQLiteThreadStore(tmp_path / "s.sqlite")
    initial = store.read_snapshot("s", "t")
    store.save_meta("s", "t", {"tui": {"title": "kept"}})
    with pytest.raises(ThreadStoreConflict):
        store.commit_operation(
            "s",
            "t",
            replace(
                operation("stale", "save_meta", meta={}),
                expected_revision=initial.revision,
            ),
        )
    session_meta = {"runtime": {"summarizing": True}, "tui": {"old": True}}
    store.patch_runtime_flag(
        "s", "t", "summarizing", active=False, session_meta=session_meta
    )
    assert session_meta == {"tui": {"title": "kept"}}
    store.append_messages("s", "t", [{"id": "first"}, {"id": "last"}])
    assert store.discard_message("s", "t", "last")
    assert store.read_snapshot("s", "t").max_seq == 1
    store.append_messages("s", "t", [{"id": "new"}])
    assert store.read_snapshot("s", "t").max_seq == 2


def test_async_adapter_conformance(tmp_path):
    from gc_core.thread_store_async import athread_store
    from gc_testkit.thread_store_conformance import assert_conforms_async

    counter = iter(range(100))
    assert_conforms_async(
        lambda: athread_store(
            SQLiteThreadStore(tmp_path / f"async-{next(counter)}.sqlite")
        )
    )


def test_process_death_rolls_back_unacknowledged_batch(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "s.sqlite"
    script = """
import os, sys
from gc_core_temporalio.storage.sqlite import SQLiteThreadStore
from gc_core.thread_store_durable import ThreadStoreOperation
class Crash(SQLiteThreadStore):
    def _after_messages(self, db):
        os._exit(73)
Crash(sys.argv[1]).commit_operation("s", "t", ThreadStoreOperation(
    "batch", "persist", {"messages": [{"id": "m1"}], "meta": {"saved": True}}))
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(path)], check=False
    )
    assert process.returncode == 73
    store = SQLiteThreadStore(path)
    assert store.read_snapshot("s", "t").state.messages == []
    assert store.read_snapshot("s", "t").meta == {}
    request = operation(
        "batch", "persist", messages=[{"id": "m1"}], meta={"saved": True}
    )
    receipt = store.commit_operation("s", "t", request)
    assert receipt.written == 1
    assert store.commit_operation("s", "t", request) == receipt


def test_parallel_process_sidecars_share_sequence(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "s.sqlite"
    SQLiteThreadStore(path)
    script = """
import sys
from gc_core_temporalio.storage.sqlite import SQLiteThreadStore
store = SQLiteThreadStore(sys.argv[1])
for index in range(5):
    store.append_subagent_trace("s", "t", parent_call_id="p", child_run_id=sys.argv[2], kind="event", payload={"i": index})
"""
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(path), str(index)])
        for index in range(2)
    ]
    assert [process.wait(timeout=20) for process in processes] == [0, 0]
    assert [
        record["seq"]
        for record in SQLiteThreadStore(path).read_subagent_trace("s", "t")
    ] == list(range(1, 11))


def test_sqlite_temporal_wire_conformance(tmp_path):
    from gc_testkit.thread_store_conformance import assert_conforms_async
    from gc_core_temporalio.thread_store_activities import (
        ThreadStoreActivities,
    )
    from gc_core_temporalio.thread_store_contracts import (
        ThreadStoreCall,
        ThreadStoreReply,
    )
    from gc_core_temporalio.thread_store_proxy import TemporalThreadStore
    from gc_core_temporalio.thread_store_transport import _STORE_ERRORS
    from temporalio.converter import DataConverter
    from temporalio.exceptions import ApplicationError

    counter = iter(range(100))

    def factory():
        store = SQLiteThreadStore(tmp_path / f"wire-{next(counter)}.sqlite")
        activities = ThreadStoreActivities({"sqlite": store})
        methods = {
            "agent.thread.load": activities.load,
            "agent.thread.commit": activities.commit,
            "agent.thread.read_trace": activities.read_trace,
        }

        async def wire(value, result_type):
            converter = DataConverter.default
            return (
                await converter.decode(
                    await converter.encode([value]), [result_type]
                )
            )[0]

        async def call(name, request):
            try:
                result = await methods[name](
                    await wire(request, ThreadStoreCall)
                )
            except ApplicationError as exc:
                raise _STORE_ERRORS[exc.type](exc.message) from exc
            return await wire(result, ThreadStoreReply)

        return TemporalThreadStore(
            "sqlite",
            call,
            capabilities=("replace_messages", "archive_and_replace"),
        )

    assert_conforms_async(factory)


def test_snapshot_read_neither_creates_rows_nor_waits_for_writer(tmp_path):
    """Статус/история по любому ID: снимок без вставки строк и без ожидания
    писателя, который держит блокировку записи."""
    path = tmp_path / "threads.sqlite"
    store = SQLiteThreadStore(path)
    snapshot = store.read_snapshot("s", "unknown")
    assert (snapshot.revision, snapshot.generation, snapshot.max_seq) == (
        0,
        0,
        0,
    )
    assert snapshot.meta == {} and snapshot.owner_id is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM threads").fetchone()[0] == 0
        assert (
            db.execute("SELECT count(*) FROM durable_control").fetchone()[0]
            == 0
        )
    writer = sqlite3.connect(path, isolation_level=None, timeout=0)
    try:
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        store.read_snapshot("s", "unknown")
        assert time.monotonic() - started < 1
    finally:
        writer.rollback()
        writer.close()
