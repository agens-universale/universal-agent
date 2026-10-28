"""Локальная сессия не трогает тред, который ведёт durable-исполнитель
(бессрочная аренда v2 — владелец workflow), и продолжает после её снятия."""

import pytest
from gc_core.thread_store import ThreadSession
from gc_core.threads.errors import OwnershipError
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import LeaseRequest, ThreadKey

KEY = ThreadKey(system="cli", thread="t")


async def _claim(tmp_path, owner="workflow"):
    store = FileThreadStore(tmp_path)
    record = await store.claim(KEY, LeaseRequest(owner=owner, ttl=None))
    return store, record.lease


async def test_local_writer_rejects_thread_owned_by_durable_executor(tmp_path):
    await _claim(tmp_path)
    with pytest.raises(OwnershipError, match="durable executor"):
        await ThreadSession.afrom_config(
            settings=None,
            system_id="cli",
            thread_id="t",
            store=FileThreadStore(tmp_path),
        )


async def test_local_writer_continues_after_durable_owner_released(tmp_path):
    store, lease = await _claim(tmp_path)
    await store.release(KEY, lease)
    session = await ThreadSession.afrom_config(
        settings=None,
        system_id="cli",
        thread_id="t",
        store=FileThreadStore(tmp_path),
    )
    assert session.thread_id == "t"
