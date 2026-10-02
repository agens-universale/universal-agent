"""One remote startup reset preserves concurrent metadata and own receipts."""

from copy import deepcopy

import pytest
from gc_core.run_summary_coordinator import areset_run_flags
from gc_core.thread_store import ThreadSession
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
from gc_ua_temporalio.thread_store_activities import ThreadStoreActivities
from gc_ua_temporalio.thread_store_contracts import (
    ThreadStoreCall,
    ThreadStoreReply,
)
from gc_ua_temporalio.thread_store_proxy import TemporalThreadStore
from gc_ua_temporalio.thread_store_transport import _STORE_ERRORS
from temporalio.exceptions import ApplicationError
from test_native_thread_store import wire


@pytest.mark.parametrize("backend", ["file", "sqlite"])
async def test_remote_batch_reads_latest_meta_and_acknowledges_one_commit(
    tmp_path, backend
):
    store = (
        DurableFileThreadStore(str(tmp_path / "files"))
        if backend == "file"
        else SQLiteThreadStore(str(tmp_path / "db"))
    )
    activities = ThreadStoreActivities({"store": store})
    calls = []

    async def call(name, request):
        decoded = await wire(request, ThreadStoreCall)
        calls.append((name, deepcopy(decoded)))
        method = (
            activities.load
            if name == "agent.thread.load"
            else activities.commit
        )
        try:
            return await wire(await method(decoded), ThreadStoreReply)
        except ApplicationError as exc:
            raise _STORE_ERRORS[exc.type](exc.message) from exc

    proxy = TemporalThreadStore(
        "store", call, capabilities=("reset_run_flags",)
    )
    session = await ThreadSession.afrom_config(
        config_dict={}, system_id="s", thread_id="t", store=proxy
    )
    store.save_meta(
        "s",
        "t",
        {
            "runtime": {"summarizing": True, "new": 1},
            "tui": {"title": "external"},
        },
    )
    calls.clear()
    await areset_run_flags(session)
    assert [name for name, _ in calls] == ["agent.thread.commit"]
    reset = calls[0][1]
    assert reset.operation.kind == "reset_run_flags"
    assert session.meta["runtime"] == {"new": 1}
    assert session.meta["tui"] == {"title": "external"}
    # This external write requires an ordinary conflict reload on subsequent
    # persistence; acknowledging reset must not pretend the earlier span was ours.
    await session.apersist_batch(
        [{"role": "user", "content": "hello", "id": "u"}],
        previous_response_id=None,
    )
    assert store.load_meta("s", "t")["tui"]["title"] == "external"
    first = store.commit_operation("s", "t", reset.operation)
    store.patch_runtime_flag("s", "t", "summarizing", active=True)
    assert store.commit_operation("s", "t", reset.operation) == first
    assert store.load_meta("s", "t")["runtime"]["summarizing"] is True


async def test_proxy_without_advertised_capability_keeps_legacy_path():
    proxy = TemporalThreadStore("custom", None)
    assert proxy.areset_run_flags is None
