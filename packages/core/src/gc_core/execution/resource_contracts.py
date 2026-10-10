"""Serializable identities for resources owned by one live effect host."""

from dataclasses import dataclass, field

from gc_core.execution.contracts import ToolOutcomeUnknown, ToolResult


class ResourceOwnerLost(ToolOutcomeUnknown):
    """The live owner is gone; reconnecting cannot restore its resource state."""


@dataclass(frozen=True)
class ResourceOwnerRef:
    worker_id: str
    process_epoch: str
    owner_id: str
    schema_version: int = 1


@dataclass(frozen=True)
class BackgroundProcessRef:
    owner: ResourceOwnerRef
    operation_id: str
    pid: int
    backend: str
    schema_version: int = 1


@dataclass
class ResourceEffectResult:
    result: ToolResult
    background_processes: list[BackgroundProcessRef] = field(
        default_factory=list
    )
    schema_version: int = 1
