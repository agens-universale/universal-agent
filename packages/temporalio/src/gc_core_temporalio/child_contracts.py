"""Serializable inputs and acknowledged outputs of the ordinary child runner."""

from dataclasses import dataclass, field
from typing import Any

from gc_core.tools.subagents.runner_preparation import PreparedChildState
from gc_core.tools.subagents.runner_transfer import SubagentTransferState

from gc_core_temporalio.contracts import (
    PreparedContext,
    RunRequest,
)


@dataclass(frozen=True)
class ChildRequest:
    start: RunRequest
    parent_workflow_id: str
    parent_context: PreparedContext
    parent_state: dict[str, Any]
    parent_transfer: SubagentTransferState
    child_context: dict[str, Any]
    tool_name: str
    arguments: dict[str, Any]
    parent_call_id: str
    #: Аренда треда родителя: трасса субагента идёт в его тред.
    store_owner_id: str | None
    store_owner_epoch: int | None


@dataclass(frozen=True)
class PreparedChild:
    context: PreparedContext
    state: PreparedChildState


@dataclass(frozen=True)
class ChildCheckpoint:
    child_id: str
    sequence: int
    state: SubagentTransferState


@dataclass(frozen=True)
class ChildResult:
    output: str
    checkpoint: ChildCheckpoint
    #: Сообщения, пришедшие ребёнку после конца его цикла: их получает родитель.
    undelivered: list[dict] = field(default_factory=list)
