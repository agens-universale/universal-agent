"""Reconciliation installs a checked receipt atomically, never repeats effects."""

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from gc_core.execution.contracts import ToolResult
from gc_core.thread_store_async import athread_store
from gc_core.thread_store_durable import ThreadStoreOperation
from gc_core.thread_store_file import FileThreadStore
from gc_ua_temporalio.native_contracts import NativeCall
from gc_ua_temporalio.native_operator import NativeOperatorActivities
from gc_ua_temporalio.native_operator_contracts import (
    NativeOperatorCall,
    NativeOperatorResolution,
    NativePendingOperation,
    operation_digest,
    result_digest,
)
from gc_ua_temporalio.storage.effects import EffectLedger
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore


@pytest.fixture
def operator(tmp_path):
    store = FileThreadStore(str(tmp_path / "threads"))
    owner = store.commit_operation(
        "s",
        "t",
        ThreadStoreOperation(
            "claim", "claim_owner", {"owner_id": "workflow/run"}
        ),
    )
    receipts = SQLiteThreadStore(str(tmp_path / "receipts.sqlite"))
    effects = EffectLedger(receipts)
    native = SimpleNamespace(
        stores={"files": athread_store(store)},
        receipts=receipts,
        effects=effects,
    )
    operator = NativeOperatorActivities(native)
    call = NativeCall(
        "profile",
        "v1",
        "workflow/run/turn-1",
        "tool-1",
        {"name": "fs_write_file"},
    )
    pending = NativePendingOperation(
        "agent.tool.execute",
        call,
        "files",
        "s",
        "t",
        owner.owner_id,
        owner.owner_epoch,
        owner.generation,
    )
    digest = operation_digest(call)
    effects.begin(
        call.owner_id, call.operation_id, str(tmp_path), digest, asdict(call)
    )
    effects.finish(call.owner_id, call.operation_id, "UNKNOWN")
    result = asdict(ToolResult("verified write result"))
    resolution = NativeOperatorResolution(
        "resolution-1",
        call.operation_id,
        digest,
        digest,
        "UNKNOWN",
        {
            "source": "file://operator-receipt",
            "note": "Observed file content and captured original output",
            "result_sha256": result_digest(result),
        },
        result,
    )
    return operator, pending, resolution, store


async def test_resolution_cas_audit_idempotency_and_canonical_receipt(
    operator,
):
    service, pending, resolution, _ = operator
    receipt = await service._resolve(NativeOperatorCall(pending, resolution))
    assert receipt["resolved"] and not receipt["duplicate"]
    assert (await service._resolve(NativeOperatorCall(pending, resolution)))[
        "duplicate"
    ]
    call = pending.call
    assert (
        service.native.receipts.operation_result(
            call.owner_id, call.operation_id, operation_digest(call)
        )
        == resolution.result
    )
    assert (
        service.native.effects.get(call.owner_id, call.operation_id)["status"]
        == "SUCCEEDED"
    )
    with service.native.receipts.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM native_resolutions").fetchone()[0]
            == 1
        )
    with pytest.raises(ValueError, match="already committed"):
        await service._resolve(
            NativeOperatorCall(
                pending, replace(resolution, request_id="different")
            )
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("digest", "stale"),
        ("ledger_digest", "stale"),
        ("expected_status", "STARTED"),
        ("evidence", {}),
        ("result", {"output": 42}),
    ],
)
async def test_bad_resolution_does_not_change_ledger(operator, field, value):
    service, pending, resolution, _ = operator
    with pytest.raises((ValueError, TypeError)):
        await service._resolve(
            NativeOperatorCall(pending, replace(resolution, **{field: value}))
        )
    assert (
        service.native.effects.get(
            pending.call.owner_id, pending.call.operation_id
        )["status"]
        == "UNKNOWN"
    )
    assert (
        service.native.receipts.operation_result(
            pending.call.owner_id,
            pending.call.operation_id,
            operation_digest(pending.call),
        )
        is None
    )


async def test_changed_thread_owner_rejects_resolution(operator):
    service, pending, resolution, store = operator
    store.commit_operation(
        "s",
        "t",
        ThreadStoreOperation(
            "release",
            "release_owner",
            owner_id=pending.thread_owner,
            owner_epoch=pending.thread_epoch,
        ),
    )
    with pytest.raises(ValueError, match="owner"):
        await service._resolve(NativeOperatorCall(pending, resolution))


async def test_resume_materializes_existing_success_without_operator_invention(
    operator,
):
    service, pending, resolution, _ = operator
    call = pending.call
    service.native.effects.finish(
        call.owner_id, call.operation_id, "SUCCEEDED", resolution.result
    )
    assert (
        await service.result(NativeOperatorCall(pending)) == resolution.result
    )
    assert (
        service.native.receipts.operation_result(
            call.owner_id, call.operation_id, operation_digest(call)
        )
        == resolution.result
    )
    with service.native.receipts.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM native_resolutions").fetchone()[0]
            == 0
        )
