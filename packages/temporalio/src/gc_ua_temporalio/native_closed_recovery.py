"""Explicit recovery of a terminal workflow's fenced thread; no execution reset."""

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace

from gc_core.thread_store_async import athread_store
from gc_core.thread_store_durable import ThreadStoreOperation
from gc_core.thread_store_file import FileThreadStore
from temporalio.client import WorkflowExecutionStatus
from temporalio.converter import value_to_type

from gc_ua_temporalio.native_contracts import NativeCall
from gc_ua_temporalio.native_operator import NativeOperatorActivities
from gc_ua_temporalio.native_operator_contracts import (
    NativeOperatorCall,
    NativePendingOperation,
)
from gc_ua_temporalio.storage.effects import EffectLedger
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore, encode


def belongs_to_run(row, owner):
    def matches(value):
        return value == owner or value.startswith(owner + "/")

    if matches(row["owner"]):
        return True
    plan = json.loads(row["plan"])
    context = plan.get("child_context")
    while isinstance(context, dict):
        if matches(context.get("parent_owner_id", "")):
            return True
        context = context.get("parent_context")
    return False


def activity_for_call(call):
    data = call.data
    if "request" in data and "circuit_breaker" in data:
        return "agent.model.request"
    if (
        "name" in data
        and "operation_id" in data
        and "arguments_digest" in data
    ):
        return "agent.tool.execute"
    if "phase" in data and "action" in data:
        return "agent.memory.execute"
    raise ValueError("Unrecognized native effect contract")


class NativeClosedRecovery:
    def __init__(self, config, client, store_id):
        self.client, self.store_id = client, store_id
        spec = config.thread_stores[store_id]
        self.store = (
            FileThreadStore(spec.path)
            if spec.kind == "file"
            else SQLiteThreadStore(spec.path)
        )
        self.receipts = SQLiteThreadStore(config.receipts_database)
        self.operator = NativeOperatorActivities(
            SimpleNamespace(
                stores={store_id: athread_store(self.store)},
                receipts=self.receipts,
                effects=EffectLedger(self.receipts),
            )
        )
        with self.receipts.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS native_thread_recoveries (
                request_id TEXT PRIMARY KEY, store_id TEXT, system_id TEXT,
                thread_id TEXT, proof TEXT, receipt TEXT)""")

    async def terminal(self, system, thread):
        snapshot = await asyncio.to_thread(
            self.store.read_snapshot, system, thread, include_state=False
        )
        if not snapshot.owner_id or "/" not in snapshot.owner_id:
            raise ValueError("Thread has no bound native workflow owner")
        workflow_id, run_id = snapshot.owner_id.rsplit("/", 1)
        description = await self.client.get_workflow_handle(
            workflow_id
        ).describe()
        allowed = {
            WorkflowExecutionStatus.COMPLETED,
            WorkflowExecutionStatus.FAILED,
            WorkflowExecutionStatus.CANCELED,
            WorkflowExecutionStatus.TERMINATED,
            WorkflowExecutionStatus.TIMED_OUT,
        }
        if description.run_id != run_id or description.status not in allowed:
            raise ValueError(
                "The exact owner must be the latest terminal Temporal run"
            )
        if description.workflow_type != "NativeAgentWorkflow":
            raise ValueError("Thread owner is not a native agent workflow")
        return snapshot, {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "status": description.status.name,
        }

    def unresolved(self, owner):
        with self.receipts.transaction() as db:
            rows = db.execute(
                "SELECT * FROM effects WHERE status IN ('STARTED','UNKNOWN')"
            ).fetchall()
        return [dict(row) for row in rows if belongs_to_run(row, owner)]

    async def inspect(self, system, thread):
        snapshot, proof = await self.terminal(system, thread)
        operations = []
        for row in self.unresolved(snapshot.owner_id):
            plan = json.loads(row["plan"])
            entry = {
                "owner": row["owner"],
                "operation_id": row["operation"],
                "ledger_digest": row["digest"],
                "status": row["status"],
                "plan": plan,
            }
            if "profile" in plan:
                call = value_to_type(NativeCall, plan)
                pending = NativePendingOperation(
                    activity_for_call(call),
                    call,
                    self.store_id,
                    system,
                    thread,
                    snapshot.owner_id,
                    snapshot.owner_epoch,
                    snapshot.generation,
                )
                entry["pending"] = asdict(pending)
            else:
                entry["required"] = (
                    "Supply the original NativeCall from Temporal activity history with this exact live effect identity"
                )
            operations.append(entry)
        return {
            **proof,
            "owner_epoch": snapshot.owner_epoch,
            "generation": snapshot.generation,
            "revision": snapshot.revision,
            "unresolved": operations,
        }

    async def resolve(self, system, thread, request: NativeOperatorCall):
        snapshot, _ = await self.terminal(system, thread)
        pending = request.pending
        if (
            pending.store_id,
            pending.system_id,
            pending.thread_id,
            pending.thread_owner,
            pending.thread_epoch,
            pending.generation,
        ) != (
            self.store_id,
            system,
            thread,
            snapshot.owner_id,
            snapshot.owner_epoch,
            snapshot.generation,
        ):
            raise ValueError(
                "Closed resolution belongs to another thread fence"
            )
        if pending.activity != activity_for_call(pending.call):
            raise ValueError("Result kind does not match the original effect")
        plan_row = {
            "owner": pending.call.owner_id,
            "plan": encode(asdict(pending.call)),
        }
        if not belongs_to_run(plan_row, snapshot.owner_id):
            raise ValueError("Operation does not belong to this terminal run")
        return await self.operator._resolve(request)

    async def release(self, system, thread, request_id, *, evidence):
        if (
            not request_id.strip()
            or not isinstance(evidence, str)
            or not evidence.strip()
        ):
            raise ValueError(
                "Stable request_id and recovery evidence are required"
            )
        with self.receipts.transaction() as db:
            old = db.execute(
                "SELECT * FROM native_thread_recoveries WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if old is not None:
            if (old["store_id"], old["system_id"], old["thread_id"]) != (
                self.store_id,
                system,
                thread,
            ) or json.loads(old["proof"])["evidence"] != evidence:
                raise ValueError("Recovery request ID reused")
            if old["receipt"] is not None:
                return json.loads(old["receipt"])
            proof = json.loads(old["proof"])
        else:
            snapshot, terminal = await self.terminal(system, thread)
            unresolved = self.unresolved(snapshot.owner_id)
            if unresolved:
                raise ValueError(
                    "Resolve uncertain effects before releasing ownership: "
                    + ", ".join(row["operation"] for row in unresolved)
                )
            proof = {
                **terminal,
                "evidence": evidence,
                "owner": snapshot.owner_id,
                "owner_epoch": snapshot.owner_epoch,
                "generation": snapshot.generation,
                "revision": snapshot.revision,
            }
            with self.receipts.transaction() as db:
                db.execute(
                    "INSERT INTO native_thread_recoveries VALUES (?,?,?,?,?,NULL)",
                    (request_id, self.store_id, system, thread, encode(proof)),
                )
        # Stable operation ID makes a crash between FileThreadStore commit and
        # audit completion recoverable without releasing a later owner.
        operation = ThreadStoreOperation(
            "operator-release:" + request_id,
            "release_owner",
            expected_revision=proof["revision"],
            generation=proof["generation"],
            owner_id=proof["owner"],
            owner_epoch=proof["owner_epoch"],
        )
        receipt = await asyncio.to_thread(
            self.store.commit_operation, system, thread, operation
        )
        result = {
            "released": True,
            "workflow_id": proof["workflow_id"],
            "run_id": proof["run_id"],
            "revision": receipt.revision,
        }
        with self.receipts.transaction() as db:
            db.execute(
                "UPDATE native_thread_recoveries SET receipt=? WHERE request_id=?",
                (encode(result), request_id),
            )
        return result
