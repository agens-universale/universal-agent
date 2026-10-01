"""Closed-run release requires a terminal exact owner and settled descendants."""

from dataclasses import asdict
from types import SimpleNamespace

import pytest
from gc_core.execution.contracts import ToolResult
from gc_core.thread_store_durable import ThreadStoreOperation
from gc_ua_temporalio.native_closed_recovery import (
    NativeClosedRecovery,
    belongs_to_run,
)
from gc_ua_temporalio.native_contracts import NativeCall
from gc_ua_temporalio.native_operator_contracts import (
    NativeOperatorCall,
    NativeOperatorResolution,
    NativePendingOperation,
    operation_digest,
    result_digest,
)
from gc_ua_temporalio.native_provider import NativeStoreConfig
from temporalio.client import WorkflowExecutionStatus


class TerminalClient:
    def __init__(self):
        self.value = SimpleNamespace(
            run_id="run",
            status=WorkflowExecutionStatus.FAILED,
            workflow_type="NativeAgentWorkflow",
        )

    def get_workflow_handle(self, workflow_id):
        assert workflow_id == "workflow"
        return self

    async def describe(self):
        return self.value


@pytest.fixture(params=["file", "sqlite"])
def recovery(tmp_path, request):
    client = TerminalClient()
    config = SimpleNamespace(
        thread_stores={
            "store": NativeStoreConfig(
                kind=request.param, path=str(tmp_path / "thread-store")
            )
        },
        receipts_database=str(tmp_path / "receipts.sqlite"),
    )
    service = NativeClosedRecovery(config, client, "store")
    service.store.commit_operation(
        "s",
        "t",
        ThreadStoreOperation(
            "claim", "claim_owner", {"owner_id": "workflow/run"}
        ),
    )
    return service, client


async def test_release_is_audited_idempotent_and_does_not_release_new_owner(
    recovery,
):
    service, _ = recovery
    first = await service.release(
        "s", "t", "release-1", evidence="Operator verified terminal owner"
    )
    assert first["released"]
    assert service.store.read_snapshot("s", "t").owner_id is None
    service.store.commit_operation(
        "s",
        "t",
        ThreadStoreOperation(
            "new-owner", "claim_owner", {"owner_id": "new/run"}
        ),
    )
    assert (
        await service.release(
            "s", "t", "release-1", evidence="Operator verified terminal owner"
        )
        == first
    )
    assert service.store.read_snapshot("s", "t").owner_id == "new/run"


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", WorkflowExecutionStatus.RUNNING),
        ("status", WorkflowExecutionStatus.CONTINUED_AS_NEW),
        ("run_id", "another-run"),
        ("workflow_type", "AgentSessionWorkflow"),
    ],
)
async def test_release_rejects_nonterminal_or_foreign_run(
    recovery, field, value
):
    service, client = recovery
    setattr(client.value, field, value)
    with pytest.raises(ValueError):
        await service.release("s", "t", "release-1", evidence="Checked")
    assert service.store.read_snapshot("s", "t").owner_id == "workflow/run"


async def test_closed_child_receipt_must_be_settled_before_release(recovery):
    service, _ = recovery
    call = NativeCall(
        "profile",
        "v1",
        "workflow/child_hash",
        "tool-1",
        {
            "operation_id": "tool-1",
            "name": "write",
            "arguments_digest": "args",
        },
        child_context={
            "parent_owner_id": "workflow/run/turn-1",
            "parent_context": None,
        },
    )
    ledger = service.operator.native.effects
    digest = operation_digest(call)
    ledger.begin(
        call.owner_id, call.operation_id, "workspace", digest, asdict(call)
    )
    ledger.finish(call.owner_id, call.operation_id, "UNKNOWN")
    inspected = await service.inspect("s", "t")
    assert inspected["unresolved"][0]["operation_id"] == "tool-1"
    with pytest.raises(ValueError, match="tool-1"):
        await service.release("s", "t", "release-1", evidence="Checked")
    snapshot = service.store.read_snapshot("s", "t")
    pending = NativePendingOperation(
        "agent.tool.execute",
        call,
        "store",
        "s",
        "t",
        snapshot.owner_id,
        snapshot.owner_epoch,
        snapshot.generation,
    )
    result = asdict(ToolResult("Known result"))
    resolution = NativeOperatorResolution(
        "settle-1",
        call.operation_id,
        digest,
        digest,
        "UNKNOWN",
        {
            "source": "captured receipt",
            "note": "Verified actual effect",
            "result_sha256": result_digest(result),
        },
        result,
    )
    assert (
        await service.resolve(
            "s", "t", NativeOperatorCall(pending, resolution)
        )
    )["resolved"]
    assert (await service.release("s", "t", "release-1", evidence="Checked"))[
        "released"
    ]


def test_unrelated_child_lineage_is_not_part_of_owner():
    assert not belongs_to_run(
        {
            "owner": "workflow/child",
            "plan": '{"child_context":{"parent_owner_id":"workflow/other/turn-1"}}',
        },
        "workflow/run",
    )
