"""The native async store contract survives the actual Temporal wire codec."""

from copy import deepcopy

import pytest
from gc_core.execution.contracts import ExecutionFailure
from gc_core.thread_store import ThreadSession
from gc_core.thread_store_durable import ThreadStoreConflict
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_testkit.memory_thread_store import MemoryThreadStore
from gc_testkit.thread_store_conformance import assert_conforms_async
from gc_ua_temporalio.thread_store_activities import ThreadStoreActivities
from gc_ua_temporalio.thread_store_contracts import (
    ThreadStoreCall,
    ThreadStoreReply,
)
from gc_ua_temporalio.thread_store_proxy import TemporalThreadStore
from gc_ua_temporalio.thread_store_transport import _STORE_ERRORS
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError


async def wire(value, result_type):
    converter = DataConverter.default
    payloads = await converter.encode([value])
    return (await converter.decode(payloads, [result_type]))[0]


def proxy_for(store):
    activities = ThreadStoreActivities({"files": store})
    methods = {
        "agent.thread.load": activities.load,
        "agent.thread.commit": activities.commit,
        "agent.thread.read_trace": activities.read_trace,
    }
    requests = []

    async def call(name, request):
        decoded = await wire(request, ThreadStoreCall)
        requests.append((name, deepcopy(decoded)))
        try:
            result = await methods[name](decoded)
        except ApplicationError as exc:
            raise _STORE_ERRORS[exc.type](exc.message) from exc
        return await wire(result, ThreadStoreReply)

    proxy = TemporalThreadStore(
        "files",
        call,
        capabilities=("replace_messages", "archive_and_replace"),
        threads_dir=store.threads_dir,
    )
    return proxy, requests


def test_native_proxy_keeps_existing_async_store_contract(tmp_path):
    counter = 0

    def factory():
        nonlocal counter
        counter += 1
        return proxy_for(DurableFileThreadStore(str(tmp_path / str(counter))))[
            0
        ]

    assert_conforms_async(factory)


@pytest.mark.asyncio
async def test_real_session_commits_history_and_meta_in_one_call(tmp_path):
    store = DurableFileThreadStore(str(tmp_path))
    proxy, requests = proxy_for(store)
    session = await ThreadSession.afrom_config(
        settings=None, system_id="cli", thread_id="native", store=proxy
    )
    message = {"id": "u1", "role": "user", "content": "Сохрани результат"}
    requests.clear()
    await session.apersist_batch([message], previous_response_id="r1")
    assert [name for name, _ in requests] == ["agent.thread.commit"]
    assert requests[0][1].operation.kind == "persist"
    assert store.load_messages("cli", "native") == [message]
    assert store.load_meta("cli", "native")["messages_count"] == 1
    assert session.persisted_messages_count == 1
    await session.apersist_batch([message], previous_response_id="r1")
    assert session.persisted_messages_count == 1
    assert session.history_messages == [message]


@pytest.mark.asyncio
async def test_session_recomputes_after_ui_patch_without_losing_fields(
    tmp_path,
):
    from gc_core.thread_store import update_tui_meta

    store = DurableFileThreadStore(str(tmp_path))
    proxy, requests = proxy_for(store)
    session = await ThreadSession.afrom_config(
        settings=None, system_id="cli", thread_id="native", store=proxy
    )
    store._call(
        update_tui_meta, str(tmp_path), "cli", "native", {"title": "Название"}
    )
    session.meta["shared"] = {"working_memory": {"knowledge": "Факт"}}
    await session.apersist_batch(
        [{"id": "u1", "role": "user", "content": "Задача"}],
        previous_response_id=None,
    )
    saved = store.load_meta("cli", "native")
    assert saved["tui"]["title"] == "Название"
    assert saved["shared"]["working_memory"]["knowledge"] == "Факт"
    commits = [r.operation for name, r in requests if name.endswith("commit")]
    assert len(commits) == 2
    assert commits[0].operation_id != commits[1].operation_id


@pytest.mark.asyncio
async def test_failed_commit_does_not_advance_session(tmp_path, monkeypatch):
    store = DurableFileThreadStore(str(tmp_path))
    proxy, _ = proxy_for(store)
    session = await ThreadSession.afrom_config(
        settings=None, system_id="cli", thread_id="native", store=proxy
    )
    before = deepcopy(
        (session.meta, session.history_messages, session.known_message_keys)
    )

    async def fail(*_args, **_kwargs):
        raise ExecutionFailure("storage unavailable")

    monkeypatch.setattr(proxy, "acommit_operation", fail)
    with pytest.raises(ExecutionFailure, match="storage unavailable"):
        await session.apersist_batch(
            [{"id": "u1", "role": "user", "content": "Задача"}],
            previous_response_id="not-saved",
        )
    assert (
        session.meta,
        session.history_messages,
        session.known_message_keys,
    ) == before
    assert session.previous_response_id is None
    assert session.persisted_messages_count == 0
    assert session.known_keys_snapshot_before_last_batch is None


@pytest.mark.asyncio
async def test_concurrent_history_writer_is_not_silently_adopted(tmp_path):
    store = DurableFileThreadStore(str(tmp_path))
    proxy, _ = proxy_for(store)
    session = await ThreadSession.afrom_config(
        settings=None, system_id="cli", thread_id="native", store=proxy
    )
    store.append_messages(
        "cli", "native", [{"id": "other", "role": "user", "content": "Чужой"}]
    )
    with pytest.raises(ThreadStoreConflict, match="history changed"):
        await session.apersist_batch(
            [{"id": "mine", "role": "user", "content": "Мой"}],
            previous_response_id=None,
        )
    assert session.history_messages == []
    assert len(store.load_messages("cli", "native")) == 1


@pytest.mark.asyncio
async def test_compaction_uses_native_session_and_one_commit(tmp_path):
    store = DurableFileThreadStore(str(tmp_path))
    proxy, requests = proxy_for(store)
    session = await ThreadSession.afrom_config(
        settings=None, system_id="cli", thread_id="native", store=proxy
    )
    await session.apersist_batch(
        [{"id": "u1", "role": "user", "content": "Длинная история"}],
        previous_response_id="r1",
    )
    requests.clear()
    result = await session.aarchive_and_compact_summary_payload(
        {"summary_message_content": "Краткая сводка", "dialog_steps_offset": 4}
    )
    assert [name for name, _ in requests] == ["agent.thread.commit"]
    assert requests[0][1].operation.kind == "compact"
    assert result["archive_data_path"]
    assert store.load_messages("cli", "native") == session.history_messages
    assert session.meta["dialog_steps_offset"] == 4
    assert session.previous_response_id is None


def test_durable_worker_refuses_legacy_store_without_breaking_local_support():
    with pytest.raises(ValueError, match="durable snapshot/commit"):
        ThreadStoreActivities({"legacy": MemoryThreadStore()})


def test_proxy_does_not_invent_optional_backend_capabilities():
    proxy = TemporalThreadStore("custom", None)
    assert proxy.areplace_messages is None
    assert proxy.aarchive_and_replace is None
    assert not hasattr(proxy, "replace_messages")


@pytest.mark.parametrize("compact", [False, True])
async def test_own_metadata_ack_avoids_conflict_and_keeps_pending_meta(
    tmp_path, compact
):
    store = DurableFileThreadStore(str(tmp_path))
    proxy, requests = proxy_for(store)
    session = await ThreadSession.afrom_config(
        settings=None, system_id="cli", thread_id="native", store=proxy
    )
    message = {"id": "u1", "role": "user", "content": "Task"}
    await session.apersist_batch([message], previous_response_id="r1")
    revision = session._store_snapshot.revision
    await proxy.apatch_runtime_flag("cli", "native", "finalizing", active=True)
    await proxy.aupdate_runtime_meta("cli", "native", {"worker_note": "own"})
    ack = proxy.metadata_ack_since("cli", "native", revision)
    assert ack.revision == revision + 2
    assert proxy.metadata_ack_since("cli", "other", revision) is None
    session.meta["shared"] = {"working_memory": {"knowledge": "pending"}}
    pending = deepcopy(session.meta)
    session._accept_metadata_ack()
    assert session.meta == pending
    assert session._store_snapshot.revision == revision + 2
    requests.clear()
    if compact:
        await session.aarchive_and_compact_summary_payload(
            {"summary_message_content": "Summary", "dialog_steps_offset": 2}
        )
    else:
        await session.apersist_batch([message], previous_response_id="r2")
    assert [name for name, _ in requests] == ["agent.thread.commit"]
    saved = store.load_meta("cli", "native")
    assert saved["runtime"]["worker_note"] == "own"
    assert saved["shared"]["working_memory"]["knowledge"] == "pending"


@pytest.mark.parametrize("external_first", [False, True])
async def test_external_metadata_is_not_covered_by_own_ack(
    tmp_path, external_first
):
    from gc_core.thread_store import update_tui_meta

    store = DurableFileThreadStore(str(tmp_path))
    proxy, requests = proxy_for(store)
    session = await ThreadSession.afrom_config(
        settings=None, system_id="cli", thread_id="native", store=proxy
    )
    if external_first:
        store._call(
            update_tui_meta,
            str(tmp_path),
            "cli",
            "native",
            {"title": "external"},
        )
    await proxy.apatch_runtime_flag("cli", "native", "finalizing", active=True)
    if not external_first:
        store._call(
            update_tui_meta,
            str(tmp_path),
            "cli",
            "native",
            {"title": "external"},
        )
    if external_first:
        assert (
            proxy.metadata_ack_since(
                "cli", "native", session._store_snapshot.revision
            )
            is None
        )
    requests.clear()
    await session.apersist_batch(
        [{"id": "u1", "role": "user", "content": "Task"}],
        previous_response_id=None,
    )
    assert [name for name, _ in requests] == [
        "agent.thread.commit",
        "agent.thread.load",
        "agent.thread.commit",
    ]
    assert store.load_meta("cli", "native")["tui"]["title"] == "external"


@pytest.mark.parametrize("observe_read", [False, True])
async def test_history_change_with_same_max_seq_cannot_be_acknowledged(
    tmp_path, observe_read
):
    store = DurableFileThreadStore(str(tmp_path))
    proxy, requests = proxy_for(store)
    session = await ThreadSession.afrom_config(
        settings=None, system_id="cli", thread_id="native", store=proxy
    )
    await session.apersist_batch(
        [{"id": "original", "role": "user", "content": "Task"}],
        previous_response_id=None,
    )
    revision = session._store_snapshot.revision
    await proxy.apatch_runtime_flag("cli", "native", "finalizing", active=True)
    changed = [{"id": "external", "role": "user", "content": "Different"}]
    if observe_read:
        store.replace_messages("cli", "native", changed)
        await proxy.aload_meta("cli", "native")
    else:
        await proxy.areplace_messages("cli", "native", changed)
    await proxy.aupdate_runtime_meta("cli", "native", {"worker_note": "own"})
    assert proxy.metadata_ack_since("cli", "native", revision) is None
    requests.clear()
    with pytest.raises(ThreadStoreConflict, match="history changed"):
        await session.apersist_batch([], previous_response_id=None)
    assert [name for name, _ in requests] == [
        "agent.thread.commit",
        "agent.thread.load",
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", 10),
        ("owner_id", "other"),
        ("owner_epoch", 10),
        ("max_seq", 10),
        ("written", 1),
        ("added_known_keys", ["unexpected"]),
        ("kind", "persist"),
        ("revision", 0),
    ],
)
async def test_session_rejects_ack_with_changed_fence(
    tmp_path, monkeypatch, field, value
):
    from gc_core.thread_store_durable import ThreadStoreReceipt

    proxy, _ = proxy_for(DurableFileThreadStore(str(tmp_path)))
    session = await ThreadSession.afrom_config(
        settings=None, system_id="cli", thread_id="native", store=proxy
    )
    snapshot = session._store_snapshot
    receipt = ThreadStoreReceipt(
        "own",
        "patch_runtime_flag",
        "digest",
        snapshot.revision + 1,
        snapshot.generation,
        max_seq=snapshot.max_seq,
        owner_id=snapshot.owner_id,
        owner_epoch=snapshot.owner_epoch,
        meta={"runtime": {"own": True}},
    )
    setattr(receipt, field, value)
    monkeypatch.setattr(proxy, "metadata_ack_since", lambda *_args: receipt)
    session._accept_metadata_ack()
    assert session._store_snapshot is snapshot
