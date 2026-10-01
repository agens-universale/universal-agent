"""Values for the native executor workflow, separate from legacy histories."""

from dataclasses import dataclass, field
from typing import Any

from gc_core.context_preparation import PreparedRunContext
from gc_core.execution.contracts import ModelResult
from gc_core.execution.resource_contracts import ResourceOwnerRef
from gc_core.tools.subagents.custom.file_definitions import SubagentDefinition

from gc_ua_temporalio.native_checkpoint_contracts import NativeCheckpointRef


@dataclass
class NativeResume:
    """Delivery receipts across Continue-As-New; agent state stays in its store."""

    human_replies: dict[str, str] = field(default_factory=dict)
    reply_receipts: dict[str, list[str]] = field(default_factory=dict)
    formatted_answer: str | None = None
    post_error: str | None = None
    closed: bool = False
    undelivered: list[dict[str, str]] = field(default_factory=list)
    accepted: dict[str, str] = field(default_factory=dict)
    pending: list[dict[str, str]] = field(default_factory=list)
    turns: int = 0
    answer: Any = None
    terminal_outcome: str | None = None
    schema_version: int = 1
    reconciliation_receipts: dict[str, list[Any]] = field(default_factory=dict)
    stop_requested: bool = False


@dataclass(frozen=True)
class NativeRunRequest:
    profile: str
    revision: str
    store_id: str
    system_id: str
    thread_id: str
    messages: list[str]
    resource_queue: str
    model_queue: str
    message_ids: list[str] | None = None
    schema_version: int = 1
    format_text: str | None = None
    keep_alive: bool = False
    continue_after_turns: int = 100
    resume: NativeResume | None = None
    resume_ref: NativeCheckpointRef | None = None


@dataclass(frozen=True)
class NativeCall:
    profile: str
    revision: str
    owner_id: str
    operation_id: str
    data: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    resource_owner: ResourceOwnerRef | None = None
    child_context: dict[str, Any] | None = None


@dataclass
class NativePreparedContext:
    context: PreparedRunContext
    tool_metadata: dict[str, dict[str, Any]]
    store_capabilities: list[str]
    threads_dir: str
    resource_owner: ResourceOwnerRef | None = None
    model_transport_configured: bool = False
    provider_currency: str = "$"
    model_provider_currencies: dict[str, str] = field(default_factory=dict)
    schema_version: int = 1
    subagent_definitions: dict[str, SubagentDefinition] = field(
        default_factory=dict
    )


@dataclass
class NativeModelReply:
    result: ModelResult
    circuit_breaker: dict[str, str] = field(default_factory=dict)
