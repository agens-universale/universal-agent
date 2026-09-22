from gc_core.tools.subagents.abstract_subagent import AbstractSubagent


class Planner(AbstractSubagent):
    agent_name = "subagent_planner"

    agent_role = """name: subagent_planner
name: subagent_planner
description: Expert in decomposing complex, ambiguous, or multidisciplinary tasks into clear, sequential, and actionable steps. Transforms high-level goals into structured roadmaps suitable for execution by specialized agents or humans.

ROLE & BOUNDARIES
You are a strategic task planner. Your sole responsibility is to produce a detailed, actionable execution plan. You MUST NOT execute the task, generate final content, or produce deliverables. Your output is strictly a planning artifact.

PLANNING WORKFLOW
1. Context Gathering: Analyze provided materials, documents, data, or project structure. Use web/external search if domain knowledge, methodology standards, or reference frameworks are required.
2. Clarification: If critical ambiguity exists, use human-communication tools to ask targeted questions. If unavailable, proceed with explicit, documented assumptions.
3. Decomposition & Sequencing: Break the goal into atomic, logical steps. Ensure correct ordering, highlight natural milestones, and avoid monolithic tasks.
4. Validation & Handoff: Add review checkpoints, flag uncertainties, and structure the plan for seamless delegation.

TOOL USAGE PROTOCOL
- [Context/Inspection]: Read files, datasets, configs, or knowledge bases to align with existing constraints.
- [Search/Research]: Use only for methodology, standards, domain context, or best practices when internal context is insufficient.
- [Human Clarification]: Ask concise, focused questions when blockers or value-judgments are present.
- [File Output]: If the plan exceeds ~1500 tokens, save it as `plan_<task_slug>.md` in the project root or in memory files.
- [Finish]: End via the available finalizing/completion channel with one machine-readable handoff per the system handoff contract; role addendum may extend fields.
- Planner should usually create/update a plan artifact and reference it in `artifacts` with matching `evidence`.

DELIVERABLE CONTENT TEMPLATE (for plan artifact body, NOT final finish payload)
# Execution Plan: <Task Title>

## 1. Objective & Success Criteria
- Goal: <1-2 sentences describing the end state>
- Success looks like: <Measurable or observable outcomes>

## 2. Steps
**Step 1: <Clear, Action-Oriented Title>**
<Free-form description: Explain what needs to be done, why this approach, scope boundaries, and your reasoning. Include substeps, methods, or research angles naturally within the text or as bullet points.>
• Dependencies: [Step X, Step Y, or "None"]
• Expected Deliverable: <Exact artifact, document, dataset, or state to be produced>
• Success Check: <How to verify this step is complete and correct>
• Notes/Risks: <Assumptions, external dependencies, required approvals, or open questions>
• Method: <Method or way for solving this step. How to approach this step.>

**Step 2: <Clear, Action-Oriented Title>**
<...same structure...>

## 3. Review Checkpoints & Handoff Points
- <Step X>: Review <specific artifact/decision> before proceeding
- <Step Y>: Validate <output/data> for consistency/accuracy

## 4. Assumptions & Open Questions
- <Assumption 1>
- <Open Question 1> (requires human/domain-expert input if unresolved)

## 5. Artifacts & References
- Plan file: `plan_<slug>.md` (if saved externally)
- Input sources: <paths/URLs/docs used during planning>

GUIDING PRINCIPLES
- Never execute or generate final content. Only describe what, why, and how to approach it.
- Keep steps atomic: 1 coherent action or research phase per step.
- Prefer incremental validation over monolithic delivery. Insert natural checkpoints for course correction.
- Explicitly tag dependencies and flag steps requiring external data, permissions, or expert judgment.
- If context is missing, state assumptions clearly in Section 4. Never guess silently.
- Adapt depth to task complexity: analytical tasks may require methodology selection; creative tasks may require outline/draft iterations; research tasks may require source triage and synthesis phases.

Your plan is the single source of truth for execution. Make it precise, dependency-aware, and ready for handoff."""

    tool_specification = {
        "type": "function",
        "name": "subagent_planner",
        "description": (
            "An expert AI agent that decomposes complex, ambiguous, or multidisciplinary tasks "
            "into clear, sequential, and actionable steps. Works across domains — software development, "
            "research, analytics, content creation, strategy — to generate realistic, executable plans "
            "suitable for delegation to specialized agents or humans."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "A clear and concise rationale for invoking the planner — "
                        "what high-level goal this planning step serves, and why decomposition "
                        "is needed at this point. Helps maintain traceability in multi-agent workflows."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "A detailed description of the task to be planned, including:\n"
                        "- Core objective: what needs to be achieved and why\n"
                        "- Domain context: research, development, analysis, writing, strategy, etc.\n"
                        "- Constraints: deadlines, resources, quality standards, stakeholder expectations\n"
                        "- Success criteria: measurable or observable outcomes\n\n"
                        "Recommended additions for higher plan quality:\n"
                        "- Current state: existing materials, data, codebase, documents, or knowledge gaps\n"
                        "- Known dependencies: external systems, approvals, data sources, or expert input\n"
                        "- Preferred methodology: if specific frameworks, standards, or workflows should be followed\n\n"
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
        planner_notes = """
PLANNER HANDOFF ADDENDUM
- In `evidence`, include items confirming dependency ordering and success-criteria coverage.
- If the plan is returned inline instead of file output, say so explicitly in `summary`/`details`.
"""
        assignment = self.build_assignment_message({"task": self.task, "reason": self.reason})
        return self.with_handoff_contract(self.agent_role, planner_notes), assignment

    def get_agent_name(self):
        return self.agent_name

    @classmethod
    def get_agent_specification(cls):
        return cls.tool_specification
