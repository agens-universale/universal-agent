"""Локальный FileThreadStore пишет как develop и не трогает тред, который
ведёт durable-исполнитель (бессрочная аренда v2 — владелец workflow)."""

import os

import pytest
from gc_core.thread_store import ThreadSession
from gc_core.thread_store_file import FileThreadStore
from gc_core.threads.errors import OwnershipError
from gc_core.threads.file_store import FileThreadStore as FileThreadStoreV2
from gc_core.threads.models import LeaseRequest, ThreadKey

KEY = ThreadKey(system="cli", thread="t")


async def _claim(tmp_path, owner="workflow"):
    store = FileThreadStoreV2(tmp_path)
    record = await store.claim(KEY, LeaseRequest(owner=owner, ttl=None))
    return store, record.lease


async def test_local_session_writes_without_service_directory(tmp_path):
    session = await ThreadSession.afrom_config(
        settings=None,
        system_id="cli",
        thread_id="t",
        store=FileThreadStore(str(tmp_path)),
    )
    await session.apersist_batch(
        [{"id": "m1", "role": "user", "content": "hi"}],
        previous_response_id=None,
    )
    assert not os.path.exists(tmp_path / ".store")
    messages = FileThreadStore(str(tmp_path)).load_messages("cli", "t")
    assert messages[0]["id"] == "m1"


async def test_local_writer_rejects_thread_owned_by_durable_executor(tmp_path):
    await _claim(tmp_path)
    with pytest.raises(OwnershipError, match="durable executor"):
        await ThreadSession.afrom_config(
            settings=None,
            system_id="cli",
            thread_id="t",
            store=FileThreadStore(str(tmp_path)),
        )


async def test_local_writer_continues_after_durable_owner_released(tmp_path):
    store, lease = await _claim(tmp_path)
    await store.release(KEY, lease)
    session = await ThreadSession.afrom_config(
        settings=None,
        system_id="cli",
        thread_id="t",
        store=FileThreadStore(str(tmp_path)),
    )
    assert session.thread_id == "t"
