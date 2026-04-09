import json
from abc import ABC, abstractmethod
from typing import Any, Dict, Tuple


class AbstractSubagent(ABC):
    WORKFLOW_ASSIGNMENT_GUIDANCE = """
WORKFLOW CONTEXT
- You are a subagent in a multi-agent workflow and should act as part of orchestrated execution.
- Assignment intent is provided as structured JSON input.
- Persist important intermediate/final knowledge so other subagents can reuse it after summarization.
- In final handoff, reference each created/updated artifact with a resolvable path:
  - absolute filesystem path for regular files;
  - memory://<artifact_id> for memory artifacts.
"""

    HANDOFF_CONTRACT_GUIDANCE = """

UNIFIED HANDOFF CONTRACT
- Final answer MUST be returned via finish tool as JSON string in finish.result.
- Required keys:
  {
    "result_schema_version": "1.0",
    "status": "pass" | "partial" | "failed",
    "report_summary": "short plain-text summary (2-5 sentences)",
    "artifacts_created": [artifact, ...],
    "artifacts_updated": [artifact, ...],
    "checks": [check, ...]
  }
- Optional keys (use when applicable): issues, metrics, recommendations, blockers.
- Artifact object shape:
  {
    "path": "absolute filesystem path OR memory://<artifact_id>",
    "kind": "doc" | "code" | "config" | "report",
    "format": "md" | "py" | "json" | "...",
    "operation": "created" | "updated",
    "evidence": "what proves success for this artifact"
  }
- If filesystem write is unavailable, store outputs in memory files and use `memory://...` path references.
- If no artifact storage is possible at all, keep artifacts arrays empty and explain limitation in report_summary plus failed/partial check evidence.
- Check object shape:
  {
    "name": "string",
    "status": "pass" | "partial" | "failed",
    "evidence": "string"
  }
- Keep handoff concise and machine-readable. Do not return prose-only final handoff.
"""

    @classmethod
    def with_handoff_contract(
        cls,
        agent_role: str,
        role_specific_handoff_notes: str | None = None,
    ) -> str:
        role = agent_role.rstrip()
        notes = (role_specific_handoff_notes or "").strip()
        workflow = cls.WORKFLOW_ASSIGNMENT_GUIDANCE.strip()
        if notes:
            return f"{role}\n{workflow}\n{cls.HANDOFF_CONTRACT_GUIDANCE}\n{notes}\n"
        return f"{role}\n{workflow}\n{cls.HANDOFF_CONTRACT_GUIDANCE}\n"

    @staticmethod
    def build_assignment_message(assignment_payload: Dict[str, Any]) -> str:
        return (
            "Assignment:\n"
            f"{json.dumps(assignment_payload, ensure_ascii=False, indent=2)}\n\n"
            "Treat this object as the source of task intent."
        )

    @classmethod
    @abstractmethod
    async def create_instance(cls, **kwargs) -> "AbstractSubagent":
        """
        Асинхронная фабрика для создания экземпляра.
        Должна быть реализована в подклассах.
        """
        ...

    @abstractmethod
    def get_initial_data(self) -> Tuple[str, str]:
        """
        Returns: Tuple[agent_role, initial_user_message]
        """
        pass

    @abstractmethod
    def get_agent_name(self) -> str: ...

    @classmethod
    @abstractmethod
    def get_agent_specification(cls) -> Dict: ...
