"""Optional durable operations for the existing ThreadStore contract.

Only data and capability resolution live here; this module performs no I/O.
Legacy stores keep the eleven original methods unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gc_core.execution.contracts import ExecutionFailure
from gc_core.thread_store_types import ThreadState


class ThreadStoreConflict(ExecutionFailure):
    """The caller must reload its snapshot and prepare a new operation."""


class ThreadStoreOwnershipError(ThreadStoreConflict):
    """The operation does not belong to the current writer/fence."""


class ThreadStoreOperationMismatch(ExecutionFailure):
    """A logical operation ID was reused with different input."""


class ThreadStoreRecoveryError(ExecutionFailure):
    """Pending file changes cannot be reconciled without losing data."""


@dataclass
class ThreadStoreSnapshot:
    meta: dict[str, Any]
    state: ThreadState | None
    revision: int = 0
    generation: int = 0
    max_seq: int = 0
    owner_id: str | None = None
    owner_epoch: int = 0
    schema_version: int = 1


@dataclass
class ThreadStoreOperation:
    operation_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    expected_revision: int | None = None
    generation: int = 0
    owner_id: str | None = None
    owner_epoch: int | None = None
    schema_version: int = 1


@dataclass
class ThreadStoreReceipt:
    operation_id: str
    kind: str
    input_digest: str
    revision: int
    generation: int
    result: Any = None
    written: int = 0
    max_seq: int = 0
    added_known_keys: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    archive: dict[str, Any] | None = None
    owner_id: str | None = None
    owner_epoch: int = 0
    schema_version: int = 1


def durable_store_capability(port: Any) -> tuple[Any, Any] | None:
    """Resolve an optional async snapshot/commit pair, without I/O."""
    snapshot = getattr(port, "aread_snapshot", None)
    commit = getattr(port, "acommit_operation", None)
    if callable(snapshot) and callable(commit):
        return snapshot, commit
    return None
