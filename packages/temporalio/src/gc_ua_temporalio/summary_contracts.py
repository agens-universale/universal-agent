"""Summary planning returns model requests and an explicit state patch."""

from dataclasses import dataclass, field
from typing import Any

from gc_core.execution.contracts import (
    ArtifactRef,
    ModelRequest,
    ToolInvocation,
    ToolResult,
)

from gc_ua_temporalio.control_contracts import JudgeDecision

SUMMARY_FIELDS = (
    "messages",
    "shared",
    "dialog_steps_offset",
    "previous_response_id",
    "last_main_prompt_tokens",
    "last_main_prompt_message_count",
    "loop_detector_window",
    "loop_detector_pending",
    "session_compact_applied",
)


@dataclass
class SummaryRequest:
    profile: str
    revision: str
    owner: str
    operation_id: str
    state: dict[str, Any]
    stage: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    mode: str = ""
    tool: ToolInvocation | None = None
    judges: list[JudgeDecision] = field(default_factory=list)
    sequence: int = 1
    schema_version: int = 1


@dataclass
class SummaryPlan:
    patch: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    judge: ModelRequest | None = None
    judge_digest: str | None = None
    trace: ArtifactRef | None = None
    result: ToolResult | None = None
