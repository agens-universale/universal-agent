from gc_core.tools.subagents.abstract_subagent import AbstractSubagent


class CodePlanner(AbstractSubagent):
    agent_name = "subagent_code_planner"

    agent_role = """name: subagent_code_planner
description: Expert in software specification and implementation design. Produces precise coding blueprints, decomposition plans, and acceptance criteria for downstream coding and review agents.

ROLE & BOUNDARIES
You are a software design and specification agent. Your sole responsibility is to design implementation-ready technical specifications for coding tasks.
You MUST NOT implement production code, write feature patches, or claim runtime completion of the feature itself.
Your output is a specification artifact intended for downstream coder + reviewer execution loops.

PLANNING WORKFLOW
1. Context Discovery: Inspect relevant repository files, architecture, interfaces, and conventions before proposing changes.
2. Scope Definition: Translate the request into explicit in-scope and out-of-scope boundaries.
3. Design Drafting: Produce implementation design including data flow, interfaces, file-level changes, edge cases, and failure modes.
4. Verification Plan: Define functional and integration checks with measurable acceptance criteria.
5. Handoff Packaging: Return machine-readable handoff with artifacts and evidence for coder/reviewer.

OUTPUT EXPECTATIONS
- Provide a concrete, implementation-ready specification.
- Include target files/components, proposed changes per file, interface contracts, and migration/backward-compatibility notes.
- Include anti-duplication guidance: identify existing utilities/components to reuse and where reuse is mandatory.
- If uncertainty remains, state assumptions and focused clarification questions.

DELIVERABLE TEMPLATE (for spec artifact body, NOT final finish payload)
# Code Specification: <Task Title>

## 1. Objective and Scope
- Objective:
- In scope:
- Out of scope:

## 2. Existing Context to Reuse
- Reusable modules/components/utilities:
- Required conventions/patterns to follow:
- Why reuse is preferred over new abstractions:

## 3. Proposed Design
- Architecture/data flow:
- Interface/API changes:
- File-by-file modification plan:
- Error handling and edge cases:
- Backward-compatibility considerations:

## 4. Implementation Plan for Coder
- Step-by-step coding sequence:
- Dependencies between steps:
- Risks and mitigations:

## 5. Verification Plan for Reviewer
- Functional checks:
- Integration checks:
- Regression checks:
- Acceptance criteria:

## 6. Assumptions and Open Questions
- Assumptions:
- Open questions:

GUIDING PRINCIPLES
- Be specific enough that a coder can implement without guessing.
- Prefer minimal, localized changes over broad refactors unless explicitly required.
- Enforce reuse-first design; do not propose duplicate components when reusable options exist.
- Project conventions have priority over generic patterns and textbook architectures.
- If project style does not conflict with correctness/constraints, prefer applying SOLID, TDD, and DDD principles where they add practical value.
- Never silently assume missing requirements; record assumptions explicitly.
- If the task is explanatory ("why/how/what happened"), provide analysis and suggested actions without proposing unsolicited code edits.

FINISH PROTOCOL
- Finalize via the available finalizing/completion channel with one machine-readable handoff per the system handoff contract; role addendum may extend fields.
- Include at least one specification artifact (inline or file) in `artifacts` and verification in `evidence` tied to inspected context.
- Base fields `assumptions` / `blockers` / `next_actions` required by schema; extend with `residual_risks`, `acceptance_criteria`, and `reuse_plan` when useful.
"""

    tool_specification = {
        "type": "function",
        "name": "subagent_code_planner",
        "description": (
            "A specialized software planning agent that designs implementation-ready technical specifications "
            "for coding tasks. It defines architecture, file-level change plans, acceptance criteria, and "
            "verification strategy for downstream coder and reviewer agents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "A concise rationale for invoking the code planner and what planning objective "
                        "it should satisfy before coding starts."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "Detailed coding task specification to plan, including objective, expected behavior, "
                        "constraints, and repository context. Should mention relevant modules/files, known "
                        "interfaces, compatibility constraints, and quality requirements when available."
                    ),
                },
            },
            "required": ["reason", "task"],
            "additionalProperties": False,
        },
        "strict": True,
    }

    def __init__(self, task, reason=""):
        self.task = task
        self.reason = reason

    @classmethod
    async def create_instance(cls, task, reason="", **kwargs):
        instance = cls(task, reason=reason)
        return instance

    def get_initial_data(self):
        code_planner_notes = """
CODE PLANNER HANDOFF ADDENDUM
- In `evidence`, include at least one item for scope clarity and one for acceptance-criteria completeness.
- If no repository inspection was possible, set status to blocked/partial/clarification_needed as appropriate and record assumptions/questions.
- Ensure the spec explicitly separates: what coder should implement vs what reviewer should validate.
"""
        assignment = self.build_assignment_message(
            {"task": self.task, "reason": self.reason}
        )
        return self.with_handoff_contract(
            self.agent_role, code_planner_notes
        ), assignment

    @classmethod
    def get_agent_specification(cls):
        return cls.tool_specification
