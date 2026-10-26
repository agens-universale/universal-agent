"""Serializable guard facts without security runtime imports."""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class GuardProbeRequest:
    kind: str
    tool_name: str
    value: str
    cwd: str
    operation: str = ""
    schema_version: int = 1


@dataclass(frozen=True)
class GuardProbeResult:
    canonical_path: str | None = None
    violation: dict[str, Any] | None = None
    protected_decision: str | None = None
    os_name: str | None = None
    shell: str | None = None
    has_venv: bool | None = None
    data: dict[str, Any] = field(default_factory=dict)
    facts_digest: str = ""
    schema_version: int = 1


@dataclass(frozen=True)
class GuardProbeCheck:
    request: GuardProbeRequest
    result: GuardProbeResult


GuardProbe = Callable[[GuardProbeRequest], Awaitable[GuardProbeResult]]
