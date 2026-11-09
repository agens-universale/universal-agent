"""User stop must not interrupt persistence retries or leave orphan tool calls."""

import asyncio
from types import SimpleNamespace

import pytest
from gc_core.processing_responses import _commit_user_stop_pairing
from gc_core.threads.errors import StoreUnavailable
from gc_core.threads.models import ThreadKey
from gc_core.threads.session_writer import RetryPolicy, SessionWriter
from gc_core.user_stop import UserStop, bind_user_stop
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore


@pytest.mark.parametrize("timing", ["before", "backoff"])
@pytest.mark.parametrize("operation", ["commit", "pairing", "side", "read"])
async def test_stop_preserves_storage_retries(
    monkeypatch, caplog, timing, operation
):
    store = MemoryThreadStore()
    key = ThreadKey(system="sdk", thread="stop-persistence")
    writer = SessionWriter(
        store,
        key,
        owner="agent",
        retry=RetryPolicy(max_attempts=2, initial_delay_sec=0.02),
    )
    call = {
        "id": "fc1",
        "type": "function_call",
        "call_id": "c1",
        "name": "mcp_action",
        "arguments": "{}",
    }
    output = {
        "id": "out1",
        "type": "function_call_output",
        "call_id": "c1",
        "output": '{"status":"cancelled_by_user"}',
    }
    await writer.acommit(append_messages=[call])
    state = SimpleNamespace(
        messages=[call],
        discard_writes_after_stop=True,
        stop_requested=True,
        allow_pairing_persist=False,
    )
    method = {
        "commit": "commit",
        "pairing": "commit",
        "side": "append_side",
        "read": "read_stream",
    }[operation]
    real = getattr(store, method)
    failed = asyncio.Event()
    attempts = []

    async def flaky(*args, **kwargs):
        attempts.append(getattr(args[1], "op_id", None))
        if len(attempts) == 1:
            failed.set()
            raise StoreUnavailable("temporary outage")
        return await real(*args, **kwargs)

    monkeypatch.setattr(store, method, flaky)
    gate = UserStop()
    gate.prepare_run(asyncio.get_running_loop(), None)

    async def persist(messages):
        await writer.acommit(append_messages=messages)

    async def run():
        with bind_user_stop(gate):
            if operation == "pairing":
                return await _commit_user_stop_pairing(
                    state, [output], persist
                )
            if operation == "commit":
                return await writer.acommit(append_messages=[output])
            if operation == "side":
                return await writer.aside(
                    append=[("cli.events", [{"id": "e1"}])]
                )
            return await writer.retried(
                lambda: store.read_stream(key, "messages"), "thread read"
            )

    if timing == "before":
        gate.request()
    task = asyncio.create_task(run())
    try:
        if timing == "backoff":
            await asyncio.wait_for(failed.wait(), 1)
            gate.request()
        result = await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await gate.await_settled(timeout=1)

    assert len(attempts) == 2
    assert len(set(attempts)) == 1  # Retry the same op_id, never a new write.
    if operation == "read":
        assert [entry.data for entry in result] == [call]
    elif operation == "side":
        assert [
            entry.data for entry in await store.read_stream(key, "cli.events")
        ] == [{"id": "e1"}]
    else:
        assert [
            entry.data for entry in await store.read_stream(key, "messages")
        ] == [call, output]
    if operation == "pairing":
        assert state.messages == [call, output]
        assert not state.allow_pairing_persist
    assert "Thread messages persist warning" not in caplog.text
