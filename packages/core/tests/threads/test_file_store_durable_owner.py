"""Тред, которым владеет durable-исполнитель (Temporal), закрыт для локального
писателя — и на старом журнале (``owner_id``), и на аренде v2 без срока."""

import asyncio
from datetime import timedelta

import pytest
from gc_core.thread_store import ThreadSession
from gc_core.thread_store_durable import (
    ThreadStoreOperation,
    ThreadStoreOwnershipError,
)
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_core.thread_store_journal import durable_owner_id
from gc_core.threads.errors import OwnershipError
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import LeaseRequest, ThreadKey

KEY = ThreadKey(system="cli", thread="t")


def _owned_by_old_journal(tmp_path, owner="wf-1"):
    DurableFileThreadStore(str(tmp_path)).commit_operation(
        "cli",
        "t",
        ThreadStoreOperation(
            "claim-1", "claim_owner", {"owner_id": owner}, generation=0
        ),
    )


def test_local_session_refuses_thread_owned_by_durable_executor(tmp_path):
    _owned_by_old_journal(tmp_path)
    with pytest.raises(ThreadStoreOwnershipError, match="durable executor"):
        asyncio.run(
            ThreadSession.afrom_config(
                settings=None,
                system_id="cli",
                thread_id="t",
                store=FileThreadStore(tmp_path),
            )
        )


def test_v2_claim_refuses_old_durable_owner(tmp_path):
    _owned_by_old_journal(tmp_path, owner="wf-1")
    store = FileThreadStore(tmp_path)
    with pytest.raises(OwnershipError):
        asyncio.run(
            store.claim(KEY, LeaseRequest(owner="host", ttl=timedelta(60)))
        )
    record = asyncio.run(store.claim(KEY, LeaseRequest(owner="wf-1")))
    assert record.lease.owner == "wf-1"


def test_untimed_v2_lease_is_visible_to_develop_writers(tmp_path):
    store = FileThreadStore(tmp_path)
    record = asyncio.run(store.claim(KEY, LeaseRequest(owner="wf-2")))
    assert durable_owner_id(str(tmp_path), "cli", "t") == "wf-2"
    asyncio.run(store.release(KEY, record.lease))
    assert durable_owner_id(str(tmp_path), "cli", "t") is None


def test_timed_v2_lease_does_not_lock_out_develop_writers(tmp_path):
    store = FileThreadStore(tmp_path)
    asyncio.run(
        store.claim(KEY, LeaseRequest(owner="host", ttl=timedelta(60)))
    )
    assert durable_owner_id(str(tmp_path), "cli", "t") is None


def test_v2_delete_refuses_thread_of_durable_executor(tmp_path):
    _owned_by_old_journal(tmp_path, owner="wf-1")
    (tmp_path / "data_cli_t.jsonl").write_text("", encoding="utf-8")
    store = FileThreadStore(tmp_path)
    with pytest.raises(OwnershipError):
        asyncio.run(store.delete(KEY, op_id="janitor"))


def test_v2_side_writes_to_durable_thread_allow_only_frontend_state(tmp_path):
    from gc_core.threads.models import RuntimeStatus, SideChange

    _owned_by_old_journal(tmp_path, owner="wf-1")
    store = FileThreadStore(tmp_path)
    asyncio.run(
        store.append_side(
            KEY, SideChange(op_id="tui", patch_frontend=("tui", {"x": 1}))
        )
    )
    for change in (
        SideChange(op_id="rt", set_runtime=RuntimeStatus(turn="running")),
        SideChange(op_id="tr", append=[("subagent_events", [{"k": 1}])]),
        SideChange(op_id="al", add_aliases=["resp_1"]),
    ):
        with pytest.raises(OwnershipError):
            asyncio.run(store.append_side(KEY, change))
