import contextlib
from dataclasses import asdict

import pytest
from gc_core import thread_store_journal as journal
from gc_core.thread_store_durable import (
    ThreadStoreConflict,
    ThreadStoreOperation,
    ThreadStoreOperationMismatch,
    ThreadStoreOwnershipError,
)
from gc_core.thread_store_file_durable import DurableFileThreadStore


@contextlib.contextmanager
def _file_read_scope(directory, stem):
    """Барьер чтения по физическому имени файла треда (без разбиения на system/thread)."""
    with journal._physical_read_scope(
        directory, journal._physical_control_path(directory, stem)
    ):
        yield


def operation(identity="first", operation_kind="append_messages", **kwargs):
    return ThreadStoreOperation(identity, operation_kind, kwargs)


def test_original_receipt_and_distinct_duplicate(tmp_path):
    store = DurableFileThreadStore(str(tmp_path))
    request = operation(
        messages=[{"id": "m1", "role": "user", "content": "hello"}],
        known_keys=None,
    )
    first = store.commit_operation("s", "t", request)
    assert first.written == 1
    assert asdict(store.commit_operation("s", "t", request)) == asdict(first)
    assert (
        store.commit_operation(
            "s", "t", operation("next", messages=request.payload["messages"])
        ).written
        == 0
    )
    with pytest.raises(ThreadStoreOperationMismatch):
        store.commit_operation("s", "t", operation(messages=[]))


def test_conflicts_and_receipt_before_fencing(tmp_path):
    store = DurableFileThreadStore(str(tmp_path))
    request = operation(messages=[{"id": "m1"}])
    first = store.commit_operation("s", "t", request)
    owner = store.commit_operation(
        "s", "t", operation("claim", "claim_owner", owner_id="workflow")
    )
    with pytest.raises(ThreadStoreOwnershipError):
        store.append_messages("s", "t", [{"id": "m2"}])
    assert store.commit_operation("s", "t", request) == first
    stale = ThreadStoreOperation(
        "stale",
        "save_meta",
        {"meta": {}},
        expected_revision=0,
        owner_id="workflow",
        owner_epoch=owner.owner_epoch,
    )
    with pytest.raises(ThreadStoreConflict):
        store.commit_operation("s", "t", stale)
    assert store.read_snapshot("s", "t").revision == owner.revision


def test_reset_retry_cannot_delete_new_thread(tmp_path):
    store = DurableFileThreadStore(str(tmp_path))
    store.append_messages("s", "t", [{"id": "before"}])
    request = operation("reset", "reset")
    first = store.commit_operation("s", "t", request)
    store.append_messages("s", "t", [{"id": "after"}])
    assert store.commit_operation("s", "t", request) == first
    assert store.load_messages("s", "t")[0]["id"] == "after"


@pytest.mark.parametrize("failure", ["prepared", "data", "control", "receipt"])
def test_reader_rolls_forward_after_lost_ack(tmp_path, monkeypatch, failure):
    store = DurableFileThreadStore(str(tmp_path))
    original = journal._write
    original_sync = journal._sync_dir
    triggered = False

    def crash(path, data):
        nonlocal triggered
        original(path, data)
        match = (
            (failure == "prepared" and path.name == "pending.json")
            or (failure == "data" and path.name.startswith("data_"))
            or (failure == "control" and path.name == "control.json")
            or (failure == "receipt" and path.parent.name == "receipts")
        )
        if match and not triggered:
            triggered = True
            raise OSError("simulated process crash")

    def crash_after_append(path):
        nonlocal triggered
        original_sync(path)
        if failure == "data" and path == tmp_path and not triggered:
            triggered = True
            raise OSError("simulated process crash")

    monkeypatch.setattr(journal, "_sync_dir", crash_after_append)
    request = operation(
        "batch",
        "persist",
        messages=[{"id": "m1"}],
        meta={"custom": {"answer": 42}},
        expected_written=1,
    )
    monkeypatch.setattr(journal, "_write", crash)
    with pytest.raises(OSError):
        store.commit_operation("s", "t", request)
    monkeypatch.setattr(journal, "_write", original)
    monkeypatch.setattr(journal, "_sync_dir", original_sync)
    snapshot = DurableFileThreadStore(str(tmp_path)).read_snapshot("s", "t")
    assert snapshot.state.messages == [{"id": "m1"}]
    assert snapshot.meta["custom"] == {"answer": 42}
    assert store.commit_operation("s", "t", request).written == 1
    assert len(store.load_messages("s", "t")) == 1


def test_sidecar_retry_and_compact_receipt(tmp_path):
    store = DurableFileThreadStore(str(tmp_path))
    trace = operation(
        "trace",
        "append_subagent_trace",
        parent_call_id="p",
        child_run_id="c",
        kind="event",
        payload={},
    )
    receipt = store.commit_operation("s", "t", trace)
    assert store.commit_operation("s", "t", trace) == receipt
    store.append_messages("s", "t", [{"id": "m1"}])
    compact = operation(
        "compact",
        "compact",
        messages=[{"id": "summary"}],
        meta={"summary": True},
        archive_suffix="fixed",
    )
    receipt = store.commit_operation("s", "t", compact)
    assert receipt.archive["archive_suffix"] == "fixed"
    assert store.commit_operation("s", "t", compact) == receipt
    assert len(store.read_subagent_trace("s", "t")) == 1


def test_legacy_record_id_is_stable(tmp_path):
    (tmp_path / "data_s_t.jsonl").write_text(
        '{"type":"message","seq":1,"payload":{"role":"user","content":"legacy"}}\n'
    )
    store = DurableFileThreadStore(str(tmp_path))
    assert (
        store.read_snapshot("s", "t").state
        == store.read_snapshot("s", "t").state
    )


def test_process_exit_after_data_write_recovers(tmp_path):
    import subprocess
    import sys

    script = """
import os, sys
from gc_core import thread_store_journal as journal
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_core.thread_store_durable import ThreadStoreOperation
original = journal._sync_dir
def crash(path):
    original(path)
    if str(path) == sys.argv[1]:
        os._exit(73)
journal._sync_dir = crash
DurableFileThreadStore(sys.argv[1]).commit_operation("s", "t", ThreadStoreOperation(
    "crash", "persist", {"messages": [{"id": "m1"}], "meta": {"saved": True}}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], check=False
    )
    assert result.returncode == 73
    snapshot = DurableFileThreadStore(str(tmp_path)).read_snapshot("s", "t")
    assert snapshot.meta["saved"] is True
    assert snapshot.state.messages == [{"id": "m1"}]


def test_processes_share_sidecar_sequence(tmp_path):
    import subprocess
    import sys

    script = """
import sys
from gc_core.thread_store_file_durable import DurableFileThreadStore
store = DurableFileThreadStore(sys.argv[1])
for index in range(6):
    store.append_subagent_trace("s", "t", parent_call_id="p", child_run_id=sys.argv[2], kind="event", payload={"i": index})
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), str(index)]
        )
        for index in range(2)
    ]
    for process in processes:
        assert process.wait(timeout=30) == 0
    records = DurableFileThreadStore(str(tmp_path)).read_subagent_trace(
        "s", "t"
    )
    assert [record["seq"] for record in records] == list(range(1, 13))


def test_partial_append_rolls_forward_without_duplicate(tmp_path, monkeypatch):
    import base64
    import json

    store = DurableFileThreadStore(str(tmp_path))
    store.append_messages("s", "t", [{"id": "existing"}])
    original = journal._recover

    def leave_prepared(directory, base):
        if (base / "pending.json").exists():
            raise OSError("crash before append")
        return original(directory, base)

    monkeypatch.setattr(journal, "_recover", leave_prepared)
    request = operation(
        "partial",
        messages=[{"id": "new"}],
        known_keys=["existing"],
        start_seq=1,
    )
    with pytest.raises(OSError):
        store.commit_operation("s", "t", request)
    manifest = json.loads(
        next((tmp_path / ".store").glob("*/pending.json")).read_text()
    )
    append = next(
        change for change in manifest["changes"] if "offset" in change
    )
    with (tmp_path / append["path"]).open("ab") as stream:
        stream.write(base64.b64decode(append["append"])[:17])
    monkeypatch.setattr(journal, "_recover", original)
    assert [message["id"] for message in store.load_messages("s", "t")] == [
        "existing",
        "new",
    ]
    assert store.commit_operation("s", "t", request).written == 1


def test_meta_and_append_do_not_read_old_history_or_archives(
    tmp_path, monkeypatch
):
    from pathlib import Path

    store = DurableFileThreadStore(str(tmp_path))
    store.append_messages("s", "t", [{"id": "existing"}])
    (tmp_path / "data_s_t__pre_summary__old.jsonl").write_bytes(b"old archive")
    original = Path.read_bytes

    def checked_read(path):
        assert not (
            path.parent == tmp_path and path.name.startswith("data_s_t")
        ), "hot writes must not read existing history/archive"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", checked_read)
    store.update_runtime_meta("s", "t", {"active": True})
    store.commit_operation(
        "s",
        "t",
        operation(
            "next", messages=[{"id": "new"}], known_keys=[], start_seq=1
        ),
    )


def test_filename_collision_rejects_different_logical_thread(tmp_path):
    store = DurableFileThreadStore(str(tmp_path))
    store.save_meta("a_b", "c", {"value": "first"})
    with pytest.raises(ThreadStoreOwnershipError):
        store.save_meta("a", "b_c", {"value": "second"})
    assert store.load_meta("a_b", "c")["value"] == "first"


def test_first_bind_cannot_adopt_other_identity_metadata(tmp_path):
    import json

    (tmp_path / "meta_a_b_c.json").write_text(
        json.dumps({"system": "a_b", "thread_id": "c"})
    )
    with pytest.raises(ThreadStoreOwnershipError):
        DurableFileThreadStore(str(tmp_path)).read_snapshot("a", "b_c")


def test_path_scope_recovers_without_guessing_identity(tmp_path, monkeypatch):
    import json

    original = journal._recover

    def leave_pending(directory, base):
        if (base / "pending.json").exists():
            raise OSError("interrupted")
        original(directory, base)

    monkeypatch.setattr(journal, "_recover", leave_pending)
    with pytest.raises(OSError):
        DurableFileThreadStore(str(tmp_path)).save_meta(
            "a_b", "c_d", {"ready": True}
        )
    monkeypatch.setattr(journal, "_recover", original)
    with _file_read_scope(str(tmp_path), "a_b_c_d"):
        assert json.loads((tmp_path / "meta_a_b_c_d.json").read_text())[
            "ready"
        ]
    with journal.thread_read_scope(str(tmp_path), "a_b", "c_d"):
        with _file_read_scope(str(tmp_path), "a_b_c_d"):
            assert DurableFileThreadStore(str(tmp_path)).load_meta(
                "a_b", "c_d"
            )["ready"]


def test_pending_new_metadata_is_recovered_before_listing(
    tmp_path, monkeypatch
):
    original = journal._recover

    def leave_pending(directory, base):
        if (base / "pending.json").exists():
            raise OSError("interrupted")
        original(directory, base)

    monkeypatch.setattr(journal, "_recover", leave_pending)
    with pytest.raises(OSError):
        DurableFileThreadStore(str(tmp_path)).save_meta(
            "s", "t", {"ready": True}
        )
    assert not (tmp_path / "meta_s_t.json").exists()
    monkeypatch.setattr(journal, "_recover", original)
    journal.recover_pending_threads(str(tmp_path))
    assert (tmp_path / "meta_s_t.json").exists()


def test_legacy_path_reader_and_exact_reset_share_barrier(tmp_path):
    # Барьер — свойство durable-хранилища: сброс идёт через журнал
    # (DurableFileThreadStore.reset). Сырая thread_store.reset_thread_files —
    # путь develop без журнала, и барьера у неё нет по замыслу.
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store = DurableFileThreadStore(str(tmp_path))
    legacy = tmp_path / "meta_legacy_id.json"
    legacy.write_text('{"value": "before"}')
    started = Event()

    def reset():
        started.set()
        return store.reset("cli", "legacy_id")

    with ThreadPoolExecutor(max_workers=1) as workers:
        with _file_read_scope(str(tmp_path), "legacy_id"):
            future = workers.submit(reset)
            assert started.wait(timeout=2)
            # Дать сбросу время пройти барьер, будь тот сломан: проверка
            # должна ловить поломку всегда, а не при удачном расписании.
            time.sleep(0.2)
            assert not future.done()
            assert legacy.exists()
        assert str(legacy) in future.result(timeout=2)
        assert not legacy.exists()


async def test_history_deleted_outside_journal_restarts_counter(tmp_path):
    """Очистка тредов в обход журнала не ломает следующую запись."""
    import glob
    import os

    from gc_core.thread_store import ThreadSession

    store = DurableFileThreadStore(str(tmp_path))
    session = await ThreadSession.afrom_config(
        settings=None, system_id="sys", thread_id="t1", store=store
    )
    await session.apersist_batch(
        [{"id": f"m{i}", "role": "user", "content": "x"} for i in range(5)],
        previous_response_id=None,
    )
    for path in glob.glob(os.path.join(str(tmp_path), "*_sys_t1*")):
        os.remove(path)
    fresh = await ThreadSession.afrom_config(
        settings=None, system_id="sys", thread_id="t1", store=store
    )
    await fresh.apersist_batch(
        [{"id": "n1", "role": "user", "content": "y"}],
        previous_response_id=None,
    )
    assert fresh.persisted_messages_count == 1
    assert [m["id"] for m in store.load_messages("sys", "t1")] == ["n1"]


def test_local_file_write_waits_for_journal_commit(tmp_path, monkeypatch):
    """Локальный FileThreadStore пишет под блокировкой журнала: запись между
    снимком журнала и коммитом раньше оставляла pending.json (тред мёртв)."""
    import threading
    import uuid

    from gc_core import thread_store_journal as journal
    from gc_core.thread_store_durable import ThreadStoreOperation
    from gc_core.thread_store_file import FileThreadStore
    from gc_core.thread_store_file_durable import DurableFileThreadStore

    local = FileThreadStore(str(tmp_path))
    durable = DurableFileThreadStore(str(tmp_path))
    local.append_messages(
        "s", "t", [{"role": "user", "content": "hi", "id": "m"}]
    )
    snapshot = durable.read_snapshot("s", "t", include_state=False)
    writer = threading.Thread(
        target=lambda: local.save_meta("s", "t", {"title": "local"})
    )
    original = journal._FileTransaction._read_after
    blocked = []

    def read_after(self):
        original(self)
        writer.start()
        writer.join(0.3)
        blocked.append(writer.is_alive())

    monkeypatch.setattr(journal._FileTransaction, "_read_after", read_after)
    durable.commit_operation(
        "s",
        "t",
        ThreadStoreOperation(
            uuid.uuid4().hex,
            "update_tui",
            {"patch": {"title": "server"}},
            generation=snapshot.generation,
        ),
    )
    writer.join(5)
    assert blocked == [True]
    assert local.load_meta("s", "t")["title"] == "local"
    assert not list(tmp_path.glob(".store/*/pending.json"))
    durable.read_snapshot("s", "t")


class _CountingStore:
    """Обёртка durable-хранилища: считает чтения снимка и коммиты."""

    def __init__(self, inner):
        self.inner, self.reads, self.commits = inner, 0, 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def read_snapshot(self, *args, **kwargs):
        self.reads += 1
        return self.inner.read_snapshot(*args, **kwargs)

    def commit_operation(self, *args, **kwargs):
        self.commits += 1
        return self.inner.commit_operation(*args, **kwargs)


async def test_idless_message_survives_revision_conflict(tmp_path):
    """Ключ сообщения без id при чтении — как при записи (до синтеза id):
    сверка после чужой записи сходится, а не упирается в конфликт."""
    import uuid

    from gc_core.thread_store import ThreadSession
    from gc_core.thread_store_durable import ThreadStoreOperation
    from gc_core.thread_store_file_durable import DurableFileThreadStore

    raw = DurableFileThreadStore(str(tmp_path))
    session = await ThreadSession.afrom_config(
        settings=None, system_id="s", thread_id="t", store=raw
    )
    message = {"role": "user", "content": "hello"}
    await session.apersist_batch([message], previous_response_id=None)
    snapshot = raw.read_snapshot("s", "t", include_state=False)
    raw.commit_operation(
        "s",
        "t",
        ThreadStoreOperation(
            uuid.uuid4().hex,
            "update_tui",
            {"patch": {"title": "x"}},
            generation=snapshot.generation,
        ),
    )
    await session.apersist_batch(
        [{"role": "assistant", "content": "a", "id": "a1"}],
        previous_response_id=None,
    )
    assert [m["content"] for m in raw.load_messages("s", "t")] == [
        "hello",
        "a",
    ]


async def test_subagent_trace_does_not_bump_parent_revision(tmp_path):
    from gc_core.thread_store import ThreadSession
    from gc_core.thread_store_async import athread_store
    from gc_core.thread_store_file_durable import DurableFileThreadStore

    store = _CountingStore(DurableFileThreadStore(str(tmp_path)))
    session = await ThreadSession.afrom_config(
        settings=None, system_id="s", thread_id="t", store=store
    )
    await session.apersist_batch(
        [{"role": "user", "content": "u", "id": "u1"}],
        previous_response_id=None,
    )
    await athread_store(store).aappend_subagent_trace(
        "s", "t", parent_call_id="c", child_run_id="r", kind="k", payload={}
    )
    reads, commits = store.reads, store.commits
    await session.apersist_batch(
        [{"role": "assistant", "content": "a", "id": "a1"}],
        previous_response_id=None,
    )
    assert (store.reads - reads, store.commits - commits) == (0, 1)


def test_durable_reset_removes_images_edits_and_old_receipts(tmp_path):
    """Durable reset убирает то же, что локальный: картинки, журнал правок и
    квитанции прошлого поколения (в них meta треда)."""
    from gc_core.thread_store import edits_journal_path
    from gc_core.thread_store_file_durable import DurableFileThreadStore

    store = DurableFileThreadStore(str(tmp_path))
    store.append_messages(
        "s", "t", [{"role": "user", "content": "x", "id": "m"}]
    )
    store.save_meta("s", "t", {"title": "private title"})
    image = tmp_path / "images" / "s_t_0123456789abcdef.png"
    image.parent.mkdir()
    image.write_bytes(b"png")
    edits = edits_journal_path(str(tmp_path), "s", "t")
    edits.write_text("{}\n")
    store.reset("s", "t")
    assert not image.exists() and not edits.exists()
    receipts = list(tmp_path.glob(".store/*/receipts/*.json"))
    assert not any("private title" in path.read_text() for path in receipts)


def test_stale_prepare_stage_is_removed_under_the_thread_lock(tmp_path):
    from gc_core.thread_store_file_durable import DurableFileThreadStore

    store = DurableFileThreadStore(str(tmp_path))
    store.append_messages(
        "s", "t", [{"role": "user", "content": "x", "id": "m"}]
    )
    base = next(tmp_path.glob(".store/*/control.json")).parent
    (base / "prepare-killed").mkdir()
    store.read_snapshot("s", "t")
    assert not list(base.glob("prepare-*"))
