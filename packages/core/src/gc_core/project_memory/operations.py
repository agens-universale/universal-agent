"""File phases of the real project-memory tool; compaction stays in core."""

from dataclasses import dataclass
from typing import Literal

from gc_core.project_memory.compaction import CompactionResult


@dataclass(frozen=True)
class ProjectMemoryRequest:
    phase: Literal["prepare", "finish"]
    action: str
    content: str = ""
    combined: str = ""
    compaction: CompactionResult | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class ProjectMemoryReply:
    output: str | None
    combined: str = ""
    baseline_updated: bool = False
    baseline_hash: str | None = None
    schema_version: int = 1
