"""Child preparation carries values; no parent resource or shared Python object."""

from dataclasses import dataclass, field

from gc_core.execution.contracts import ModelRequest, ToolResult

from gc_ua_temporalio.contracts import SessionStart, ToolActivityInput
from gc_ua_temporalio.control_contracts import JudgeDecision


@dataclass
class PrepareChild:
    owner: str
    tool: ToolActivityInput
    parent: SessionStart
    state: dict
    sequence: int = 1
    judges: list[JudgeDecision] = field(default_factory=list)
    schema_version: int = 1


@dataclass
class ChildPlan:
    start: SessionStart | None = None
    judge: ModelRequest | None = None
    judge_digest: str | None = None
    result: ToolResult | None = None
    model_profile: str | None = None
    model_revision: str | None = None
