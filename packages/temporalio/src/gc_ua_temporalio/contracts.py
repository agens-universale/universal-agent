"""Workflow wire inputs contain references, never provider credentials."""

from dataclasses import dataclass, field
from typing import Any

from gc_core.execution.contracts import (
    ArtifactRef,
    ModelRequest,
    ToolInvocation,
)


@dataclass
class SessionStart:
    thread_id: str
    prompt: str
    profile: str = "demo"
    profile_revision: str = "v1"
    system_id: str = "temporal"
    resource_queue: str = "ua-resource-local"
    model_queue: str = "ua-model"
    auto_close: bool = False
    resume_imported: bool = False
    schema_version: int = 1
    checkpoint: ArtifactRef | None = None
    session_owner: str | None = None
    continue_after_turns: int = 20
    child_seed: dict[str, Any] | None = None
    initial_message_id: str | None = None


@dataclass
class BootstrapRequest:
    start: SessionStart
    owner: str
    schema_version: int = 1


@dataclass
class BootstrapSnapshot:
    state: dict[str, Any]
    config: dict[str, Any]
    tool_metadata: dict[str, Any]
    meta: dict[str, Any]
    epoch: int
    max_seq: int
    history: list[dict[str, Any]] = field(default_factory=list)
    schema_version: int = 1


@dataclass
class ModelActivityInput:
    profile: str
    revision: str
    owner: str
    request: ModelRequest
    schema_version: int = 1


@dataclass
class ToolActivityInput:
    profile: str
    revision: str
    owner: str
    invocation: ToolInvocation
    schema_version: int = 1
