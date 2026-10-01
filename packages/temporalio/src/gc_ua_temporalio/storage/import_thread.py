"""Offline migration. The caller must first stop every writer of the source."""

import hashlib

from gc_core.execution.contracts import ExecutionFailure
from gc_core.persistence.schema import validate_thread_meta_for_load
from gc_core.thread_store_async import athread_store

from gc_ua_temporalio.storage.sqlite import encode


async def import_stopped_thread(
    source, target, system_id, thread_id, *, writers_stopped=False
):
    if not writers_stopped:
        raise ExecutionFailure(
            "Stop all source writers before importing the thread"
        )
    port = athread_store(source)
    meta = await port.aload_meta(system_id, thread_id)
    validate_thread_meta_for_load(meta)
    runtime = meta.get("runtime") or {}
    if any(
        runtime.get(key)
        for key in (
            "running",
            "summarizing",
            "finalizing",
            "finish_gate_checking",
        )
    ):
        raise ExecutionFailure("Source thread still has active runtime flags")
    state = await port.aload_thread_state(system_id, thread_id)
    calls = {
        m.get("call_id")
        for m in state.messages
        if m.get("type") == "function_call"
    }
    outputs = {
        m.get("call_id")
        for m in state.messages
        if m.get("type") == "function_call_output"
    }
    if calls - outputs:
        raise ExecutionFailure(
            "Reconcile unfinished legacy tool calls before migration"
        )
    traces = await port.aread_subagent_trace(system_id, thread_id)
    original_meta = {
        k: v
        for k, v in meta.items()
        if k not in ("execution_owner", "updated_at")
    }
    digest = hashlib.sha256(
        encode([state.messages, original_meta, traces]).encode()
    ).hexdigest()
    marker = {"backend": "temporal", "import_digest": digest}
    if meta.get("execution_owner") not in (None, marker):
        raise ExecutionFailure(
            "Source thread already belongs to another migration"
        )
    # Mark first: a crash before target commit leaves a resumable, blocked source.
    # There is deliberately no automatic unlock back to local execution.
    await port.asave_meta(
        system_id, thread_id, {**meta, "execution_owner": marker}
    )
    target.import_snapshot(
        system_id, thread_id, digest, state.messages, original_meta, traces
    )
    return {
        "digest": digest,
        "messages": len(state.messages),
        "traces": len(traces),
    }
