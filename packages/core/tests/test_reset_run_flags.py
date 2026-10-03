"""Startup batching reads fresh metadata and retains custom-store behavior."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from gc_core.run_summary_coordinator import (
    areset_run_flags,
    areset_stale_summarizing_flag,
    aset_tui_finalizing,
    aset_tui_finish_gate_checking,
)
from gc_core.thread_store import ThreadSession
from gc_core.thread_store_async import athread_store
from gc_core.thread_store_durable import ThreadStoreOperation
from gc_core.thread_store_file import FileThreadStore
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_testkit.memory_thread_store import MemoryThreadStore


@pytest.mark.parametrize("disk_flag", [None, False, True])
@pytest.mark.parametrize("cached_flag", [None, False, True])
async def test_batch_matches_original_flags_and_tui(
    tmp_path, disk_flag, cached_flag
):
    stores = [
        DurableFileThreadStore(str(tmp_path / "batch")),
        FileThreadStore(str(tmp_path / "legacy")),
    ]
    meta = {
        "runtime": {
            "finalizing": True,
            "finish_gate_checking": True,
            "external": 9,
        },
        "tui": {"title": "current"},
    }
    if disk_flag is not None:
        meta["runtime"]["summarizing"] = disk_flag
    sessions = []
    for store in stores:
        store.save_meta("s", "t", deepcopy(meta))
        cached = {"runtime": {}, "tui": {"title": "stale"}, "pending": "keep"}
        if cached_flag is not None:
            cached["runtime"]["summarizing"] = cached_flag
        sessions.append(
            SimpleNamespace(
                port=athread_store(store),
                meta=cached,
                system_id="s",
                thread_id="t",
            )
        )
    await areset_run_flags(sessions[0])
    await areset_stale_summarizing_flag(sessions[1])
    await aset_tui_finalizing(sessions[1], False)
    await aset_tui_finish_gate_checking(sessions[1], False)
    assert sessions[0].meta == sessions[1].meta
    actual, expected = [store.load_meta("s", "t") for store in stores]
    for key in ("runtime", "tui"):
        assert actual.get(key) == expected.get(key)
    assert actual["runtime"]["external"] == 9


async def test_external_write_between_open_and_batch_is_preserved(tmp_path):
    store = DurableFileThreadStore(str(tmp_path))
    session = await ThreadSession.afrom_config(
        settings=None, system_id="s", thread_id="t", store=store
    )
    store.save_meta(
        "s",
        "t",
        {
            "runtime": {"summarizing": True, "new_external": 7},
            "tui": {"title": "new", "pinned": True},
        },
    )
    before = store.read_snapshot("s", "t", include_state=False)
    await areset_run_flags(session)
    after = store.read_snapshot("s", "t", include_state=False)
    assert after.revision == before.revision + 1
    assert after.meta["runtime"] == {"new_external": 7}
    assert after.meta["tui"] == {"title": "new", "pinned": True}
    assert session.meta["runtime"] == after.meta["runtime"]
    assert session.meta["tui"] == after.meta["tui"]


def test_reset_batch_retry_returns_original_receipt(tmp_path):
    store = DurableFileThreadStore(str(tmp_path))
    store.save_meta("s", "t", {"runtime": {"summarizing": True}})
    operation = ThreadStoreOperation("reset-flags", "reset_run_flags", {})
    original = store.commit_operation("s", "t", operation)
    store.patch_runtime_flag("s", "t", "summarizing", active=True)
    assert store.commit_operation("s", "t", operation) == original
    assert store.load_meta("s", "t")["runtime"]["summarizing"] is True


async def test_custom_store_without_batch_keeps_original_calls():
    class RecordingStore(MemoryThreadStore):
        def __init__(self):
            super().__init__()
            self.recorded = []

        def load_meta(self, *args):
            self.recorded.append("load")
            return super().load_meta(*args)

        def patch_runtime_flag(self, *args, **kwargs):
            self.recorded.append(args[2])
            return super().patch_runtime_flag(*args, **kwargs)

    store = RecordingStore()
    port = athread_store(store)
    assert port.areset_run_flags is None
    session = SimpleNamespace(port=port, meta={}, system_id="s", thread_id="t")
    await areset_run_flags(session)
    assert store.recorded[0] == "load"
    assert [name for name in store.recorded if name != "load"] == [
        "finalizing",
        "finish_gate_checking",
    ]
