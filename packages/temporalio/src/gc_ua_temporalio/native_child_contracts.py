"""Serializable inputs and acknowledged outputs of the ordinary child runner."""

from dataclasses import dataclass, field
from typing import Any

from gc_core.tools.subagents.runner_preparation import PreparedChildState
from gc_core.tools.subagents.runner_transfer import SubagentTransferState

from gc_ua_temporalio.native_contracts import (
    NativePreparedContext,
    NativeRunRequest,
)


@dataclass(frozen=True)
class NativeChildRequest:
    start: NativeRunRequest
    parent_workflow_id: str
    parent_context: NativePreparedContext
    parent_state: dict[str, Any]
    parent_transfer: SubagentTransferState
    child_context: dict[str, Any]
    tool_name: str
    arguments: dict[str, Any]
    parent_call_id: str
    store_owner_id: str | None
    store_owner_epoch: int | None
    store_generation: int


@dataclass(frozen=True)
class NativePreparedChild:
    context: NativePreparedContext
    state: PreparedChildState


@dataclass(frozen=True)
class NativeChildCheckpoint:
    child_id: str
    sequence: int
    state: SubagentTransferState


@dataclass(frozen=True)
class NativeChildResult:
    output: str
    checkpoint: NativeChildCheckpoint
    #: Сообщения, пришедшие ребёнку после конца его цикла: их получает родитель.
    undelivered: list[dict] = field(default_factory=list)
