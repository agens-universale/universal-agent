"""Audited receipt installation; never invokes a tool, model or legacy engine."""

import hashlib
import json
from dataclasses import asdict, fields, is_dataclass
from typing import get_type_hints

from gc_core.execution.contracts import ToolResult
from gc_core.execution.resource_contracts import ResourceEffectResult
from gc_core.project_memory.operations import ProjectMemoryReply
from temporalio import activity
from temporalio.converter import value_to_type
from temporalio.exceptions import ApplicationError

from gc_ua_temporalio.native_contracts import NativeModelReply
from gc_ua_temporalio.native_operator_contracts import (
    NativeOperatorCall,
    operation_digest,
    result_digest,
)
from gc_ua_temporalio.storage.sqlite import encode

RESULT_TYPES = {
    "agent.model.request": NativeModelReply,
    "agent.tool.execute": ToolResult,
    "agent.memory.execute": ProjectMemoryReply,
}


def _result_shape(cls, value):
    if is_dataclass(cls) and isinstance(value, dict):
        if set(value) - {field.name for field in fields(cls)}:
            raise ValueError("Unknown canonical result field")
        for key, annotation in get_type_hints(cls).items():
            if key in value:
                _result_shape(annotation, value[key])


def validate_result(kind, result):
    expected = RESULT_TYPES.get(kind)
    if expected is None:
        raise ValueError(
            "Operation has no supported canonical reconciliation contract"
        )

    _result_shape(expected, result)
    decoded = value_to_type(expected, result)
    if getattr(decoded, "schema_version", 1) != 1:
        raise ValueError("Unsupported result schema")
    if kind == "agent.model.request" and decoded.result.schema_version != 1:
        raise ValueError("Unsupported model result schema")
    canonical = asdict(decoded)
    if canonical != result:
        raise ValueError(
            "Supply the complete canonical receipt without omitted fields"
        )
    return canonical


def _validate_resolution(pending, resolution):
    if resolution is None:
        raise ValueError("Resolution is required")
    call = pending.call
    digest = operation_digest(call)
    if (
        resolution.operation_id != call.operation_id
        or resolution.digest != digest
    ):
        raise ValueError("Stale operation identity")
    if not resolution.request_id.strip():
        raise ValueError("Stable resolution request_id is required")
    evidence = resolution.evidence
    if set(evidence) != {"source", "note", "result_sha256"} or not all(
        isinstance(v, str) and v.strip() for v in evidence.values()
    ):
        raise ValueError(
            "Evidence source, note and result_sha256 are required"
        )
    if evidence["result_sha256"] != result_digest(resolution.result):
        raise ValueError("Evidence result digest mismatch")
    result = validate_result(pending.activity, resolution.result)
    return result


def _ledger_result(resolution, result, live):
    ledger_result = result
    if live:
        if resolution.ledger_result is None:
            raise ValueError(
                "Live resource resolution requires the complete resource receipt"
            )
        resource_result = value_to_type(
            ResourceEffectResult, resolution.ledger_result
        )
        if asdict(resource_result.result) != result:
            raise ValueError("Resource and canonical tool receipts disagree")
        ledger_result = asdict(resource_result)
    elif resolution.ledger_result is not None:
        raise ValueError(
            "Unexpected resource receipt for a stateless operation"
        )
    return ledger_result


class NativeOperatorActivities:
    def __init__(self, native):
        self.native = native
        with native.receipts.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS native_resolutions (
                owner TEXT, operation TEXT, request_id TEXT, digest TEXT,
                payload TEXT, PRIMARY KEY(owner,operation), UNIQUE(owner,request_id))""")

    async def _owner(self, pending):
        snapshot = await self.native.stores[pending.store_id].aread_snapshot(
            pending.system_id, pending.thread_id, include_state=False
        )
        if (snapshot.owner_id, snapshot.owner_epoch, snapshot.generation) != (
            pending.thread_owner,
            pending.thread_epoch,
            pending.generation,
        ):
            raise ValueError("Thread owner or generation changed")

    @staticmethod
    def _row(db, pending):
        call = pending.call
        row = db.execute(
            "SELECT * FROM effects WHERE owner=? AND operation=?",
            (call.owner_id, call.operation_id),
        ).fetchone()
        if row is not None and json.loads(row["plan"]) == asdict(call):
            if row["digest"] != operation_digest(call):
                raise ValueError(
                    "Effect identity does not match suspended native operation"
                )
            return dict(row), False
        if pending.activity != "agent.tool.execute":
            raise ValueError("No bound native effect receipt exists")
        resource_owner = call.owner_id
        parent = call.child_context
        while parent is not None:
            resource_owner = parent["parent_owner_id"]
            parent = parent.get("parent_context")
        if call.resource_owner is not None:
            resource_owner = call.resource_owner.owner_id
        row = db.execute(
            "SELECT * FROM effects WHERE owner=? AND operation=?",
            (resource_owner, call.data["operation_id"]),
        ).fetchone()
        if row is None:
            raise ValueError("No bound live effect receipt exists")
        plan = json.loads(row["plan"])
        ref = plan.get("resource_owner", {})
        if (
            ref.get("owner_id") != resource_owner
            or plan.get("effect") != call.data
        ):
            raise ValueError("Live effect identity changed")
        if call.resource_owner is not None and ref != asdict(
            call.resource_owner
        ):
            raise ValueError("Live owner epoch changed")
        payload = (
            call.data
            if call.child_context is None
            else {"effect": call.data, "context_id": call.owner_id}
        )
        expected = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        if row["digest"] != expected:
            raise ValueError("Live effect digest changed")
        return dict(row), True

    @activity.defn(name="agent.operator.inspect")
    async def inspect(self, request: NativeOperatorCall) -> dict:
        try:
            await self._owner(request.pending)
            with self.native.receipts.transaction() as db:
                row, live = self._row(db, request.pending)
            return {
                **request.pending.public(),
                "ledger_digest": row["digest"],
                "status": row["status"],
                "live_resource": live,
                "result": self.native.effects.result(row),
            }
        except ValueError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc

    @activity.defn(name="agent.operator.resolve")
    async def resolve(self, request: NativeOperatorCall) -> dict:
        try:
            return await self._resolve(request)
        except ValueError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc

    async def _resolve(self, request):
        pending, resolution = request.pending, request.resolution
        result = _validate_resolution(pending, resolution)
        call = pending.call
        digest = operation_digest(call)
        await self._owner(pending)
        encoded = encode(asdict(resolution))
        with self.native.receipts.transaction() as db:
            old = db.execute(
                "SELECT payload FROM native_resolutions WHERE owner=? AND operation=?",
                (call.owner_id, call.operation_id),
            ).fetchone()
            if old is not None:
                if old["payload"] != encoded:
                    raise ValueError(
                        "Resolution already committed with different evidence or result"
                    )
                return {
                    "operation_id": call.operation_id,
                    "resolved": True,
                    "duplicate": True,
                }
            row, live = self._row(db, pending)
            if (
                row["digest"] != resolution.ledger_digest
                or row["status"] != resolution.expected_status
            ):
                raise ValueError("Stale effect status or digest")
            if row["status"] not in {"STARTED", "UNKNOWN"}:
                raise ValueError("Only uncertain effects can be reconciled")
            existing = db.execute(
                "SELECT digest,result FROM operations WHERE owner=? AND operation=?",
                (call.owner_id, call.operation_id),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    "Canonical operation result is already present"
                )
            ledger_result = _ledger_result(resolution, result, live)
            db.execute(
                "UPDATE effects SET status='SUCCEEDED',result=? WHERE owner=? AND operation=? AND digest=? AND status=?",
                (
                    encode(ledger_result),
                    row["owner"],
                    row["operation"],
                    row["digest"],
                    row["status"],
                ),
            )
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,?)",
                (call.owner_id, call.operation_id, digest, encode(result)),
            )
            db.execute(
                "INSERT INTO native_resolutions VALUES (?,?,?,?,?)",
                (
                    call.owner_id,
                    call.operation_id,
                    resolution.request_id,
                    digest,
                    encoded,
                ),
            )
        return {
            "operation_id": call.operation_id,
            "resolved": True,
            "duplicate": False,
        }

    @activity.defn(name="agent.operator.result")
    async def result(self, request: NativeOperatorCall) -> dict:
        try:
            await self._owner(request.pending)
            call = request.pending.call
            result = self.native.receipts.operation_result(
                call.owner_id, call.operation_id, operation_digest(call)
            )
            if result is None:
                with self.native.receipts.transaction() as db:
                    row, live = self._row(db, request.pending)
                    if row["status"] != "SUCCEEDED" or row["result"] is None:
                        raise ValueError(
                            "No verified canonical receipt is available"
                        )
                    result = json.loads(row["result"])
                    if live:
                        result = result["result"]
                    result = validate_result(request.pending.activity, result)
                    db.execute(
                        "INSERT OR IGNORE INTO operations VALUES (?,?,?,?)",
                        (
                            call.owner_id,
                            call.operation_id,
                            operation_digest(call),
                            encode(result),
                        ),
                    )
            return validate_result(request.pending.activity, result)
        except ValueError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
