"""Локальный FileThreadStore пишет как develop; durable-вариант — только по выбору."""

import os

import pytest
from gc_core.thread_store import ThreadSession
from gc_core.thread_store_async import athread_store
from gc_core.thread_store_durable import (
    ThreadStoreOperation,
    ThreadStoreOwnershipError,
    durable_store_capability,
)
from gc_core.thread_store_file import FileThreadStore
from gc_core.thread_store_file_durable import DurableFileThreadStore


def _claim(store, owner="workflow"):
    store.commit_operation(
        "cli",
        "t",
        ThreadStoreOperation("claim", "claim_owner", {"owner_id": owner}),
    )


def test_plain_file_store_has_no_durable_capability(tmp_path):
    plain = athread_store(FileThreadStore(str(tmp_path)))
    durable = athread_store(DurableFileThreadStore(str(tmp_path)))
    assert durable_store_capability(plain) is None
    assert durable_store_capability(durable) is not None


async def test_local_session_writes_without_service_directory(tmp_path):
    session = await ThreadSession.afrom_config(
        config_dict={},
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
    _claim(DurableFileThreadStore(str(tmp_path)))
    with pytest.raises(ThreadStoreOwnershipError):
        await ThreadSession.afrom_config(
            config_dict={},
            system_id="cli",
            thread_id="t",
            store=FileThreadStore(str(tmp_path)),
        )


async def test_local_writer_continues_after_durable_owner_released(tmp_path):
    durable = DurableFileThreadStore(str(tmp_path))
    _claim(durable)
    durable.commit_operation(
        "cli",
        "t",
        ThreadStoreOperation(
            "release", "release_owner", {}, owner_id="workflow"
        ),
    )
    session = await ThreadSession.afrom_config(
        config_dict={},
        system_id="cli",
        thread_id="t",
        store=FileThreadStore(str(tmp_path)),
    )
    assert session.thread_id == "t"
