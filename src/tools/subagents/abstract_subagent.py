import json
from abc import ABC, abstractmethod
from typing import Any, Dict, Tuple


class AbstractSubagent(ABC):
    WORKFLOW_ASSIGNMENT_GUIDANCE = """
WORKFLOW CONTEXT
- You are a subagent in a multi-agent workflow and should act as part of orchestrated execution.
- Assignment intent is provided as structured JSON input.
- Your child conversation is NOT visible to the parent orchestrator or to other subagents.
- Thread persistence uses shared thread-memory tools from `<tools>` (built-in or MCP) — typically the only cross-agent context besides the submit report. The parent dialog may be summarized later; your trace will not survive that.
- At run start: read thread memory relevant to the assignment when such tools are available.
- Before final handoff: append durable outcomes to thread memory (decisions, verified facts, blockers, artifact paths). The submit report summarizes; thread memory is what must survive summarization when present.
- Do not assume another agent remembers your work unless it is recorded in thread memory.
- In final handoff, reference each created/updated artifact with a resolvable path:
  - absolute filesystem path for regular files;
  - memory://<artifact_id> for memory artifacts.
"""

    HANDOFF_CONTRACT_GUIDANCE = """

HANDOFF CONTRACT
- End the run exactly once through the available finalizing/completion channel from `<tools>`.
- Put one machine-readable handoff in that channel's handoff/payload slot (shape and encoding follow the tool schema and the system handoff contract).
- Treat the system prompt's handoff contract as the single source of truth for keys and status values; do not invent a parallel schema.
- Role addendum may require extra fields; extend the system contract — do not rename or replace its base keys/enums.
- Keep handoff concise (brief self-contained summary + structured fields). Put the full narrative in the finalizing tool's dedicated human-facing report/summary field — do not duplicate a long write-up in handoff.
- Keep handoff machine-readable. Do not return prose-only final handoff.
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
