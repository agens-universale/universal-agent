"""Resource-session identity and MCP call references, without transport handles."""

from dataclasses import dataclass

from gc_ua_temporalio.contracts import ToolActivityInput
from gc_ua_temporalio.control_contracts import ReconcileRequest


@dataclass
class McpSessionRef:
    session_id: str
    generation: str
    server: str
    revision: str


@dataclass
class McpCall:
    tool: ToolActivityInput
    session: McpSessionRef
    tool_name: str
    schema: dict
    resolution: ReconcileRequest | None = None
    schema_version: int = 1


@dataclass
class ReleaseMcp:
    owner: str
    sessions: list[McpSessionRef]
