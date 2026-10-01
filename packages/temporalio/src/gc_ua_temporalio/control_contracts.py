"""Wire values for guarded tools and durable human interaction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gc_core.execution.contracts import (
    ArtifactRef,
    ModelRequest,
    ModelResult,
    ToolResult,
)
from gc_core.execution.human import HumanRequest

from gc_ua_temporalio.contracts import ToolActivityInput


@dataclass
class HumanDecision:
    digest: str
    answer: str


@dataclass
class JudgeDecision:
    digest: str
    result: ModelResult


@dataclass
class PrepareToolInput:
    tool: ToolActivityInput
    decisions: list[HumanDecision] = field(default_factory=list)
    sequence: int = 1
    schema_version: int = 1
    judges: list[JudgeDecision] = field(default_factory=list)

    @property
    def owner(self):
        return self.tool.owner


@dataclass
class PreparedTool:
    fingerprint: str
    request: HumanRequest | None = None
    request_digest: str | None = None
    result: ToolResult | None = None
    path: str | None = None
    before: dict[str, Any] = field(default_factory=dict)
    content: ArtifactRef | None = None
    schema_version: int = 1
    judge: ModelRequest | None = None
    judge_digest: str | None = None
    effect_kind: str = "file"
    effect_call: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecuteEffectInput:
    prepared: PrepareToolInput
    plan: PreparedTool
    schema_version: int = 1
    resolution: ReconcileRequest | None = None


@dataclass
class EffectResult:
    status: str
    result: ToolResult | None = None
    detail: str = ""
    schema_version: int = 1


@dataclass
class HumanReply:
    request_id: str
    reply_id: str
    answer: str


@dataclass
class ReconcileRequest:
    operation_id: str
    request_id: str
    resolution: str = "inspect"
    output: str = ""
    processes_stopped: bool = False
