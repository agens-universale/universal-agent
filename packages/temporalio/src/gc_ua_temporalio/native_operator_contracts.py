"""Explicit evidence-based reconciliation of one suspended native operation."""

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from gc_ua_temporalio.native_contracts import NativeCall


def operation_digest(call: NativeCall) -> str:
    return hashlib.sha256(
        json.dumps(asdict(call), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def result_digest(result: Any) -> str:
    return hashlib.sha256(
        json.dumps(result, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@dataclass
class NativePendingOperation:
    activity: str
    call: NativeCall
    store_id: str
    system_id: str
    thread_id: str
    thread_owner: str
    thread_epoch: int
    generation: int

    def public(self):
        return {
            "activity": self.activity,
            "owner_id": self.call.owner_id,
            "operation_id": self.call.operation_id,
            "digest": operation_digest(self.call),
        }


@dataclass
class NativeOperatorResolution:
    request_id: str
    operation_id: str
    digest: str
    ledger_digest: str
    expected_status: str
    evidence: dict[str, str]
    result: dict[str, Any]
    ledger_result: dict[str, Any] | None = None


@dataclass
class NativeOperatorCall:
    pending: NativePendingOperation
    resolution: NativeOperatorResolution | None = None


@dataclass
class NativeOperatorResume:
    operation_id: str
    digest: str
