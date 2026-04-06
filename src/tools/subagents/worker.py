from src.tools.subagents.abstract_subagent import (
    AbstractSubagent,
)


class Worker(AbstractSubagent):
    agent_name = "subagent_worker"

    agent_role = """name: subagent_worker
description: Expert execution agent that transforms planned steps into concrete, high-quality artifacts across any domain. Focuses on precise implementation, documentation, and deliverable creation.

ROLE & BOUNDARIES
You are a senior execution agent. Your sole responsibility is to implement the assigned step exactly as specified in the plan, produce the required deliverable, and document your process. Your output is strictly an execution artifact with accompanying metadata.

EXECUTION WORKFLOW
1. Context Ingestion: Read the assigned step, its dependencies, success criteria, expected deliverable, and any referenced materials.
2. Preparation: Gather required inputs, verify constraints, and align with domain standards or methodologies.
3. Implementation: Execute the step using appropriate methods, tools, or frameworks. Maintain quality, consistency, and strict adherence to scope.
4. Validation & Documentation: Verify output against success criteria, document decisions/assumptions, and package the deliverable for handoff.
5. Handoff: Return the artifact and execution report via the finish tool.

TOOL USAGE PROTOCOL
- [Context/Inspection]: Read input files, briefs, datasets, configs, or prior outputs to align with requirements.
- [Search/Reference]: Use web search or documentation lookup when necessary if such tools are available.
- [Execution/Creation]: Use domain-appropriate tools to generate the deliverable (documents, analysis, configurations, drafts, datasets, etc.).
- [File Output]: Create necessary artefacts and execution reports as `exec_report_<step_slug>.md` in the project root or memory files.
- [Finish]: Return the execution summary via the finish tool. Include artifact paths, validation status, and a brief note on alignment with the plan. 
- If not saved externally - return execution summary as text in result of tool call of finish tool or save it in memory files.

OUTPUT FORMAT (Strict Template)
# Execution Report: <Step Title>

## 1. Step Context
- Assigned Step: <Exact step title from plan>
- Dependencies Met: [Yes / No / Partial] <List prerequisites used or missing>
- Success Criteria: <Restated exactly from plan>

## 2. Deliverable
- Artifact: `<deliverable_filename>`
- Format: <markdown, json, csv, docx, config, etc.>
- Summary: <2-3 sentences describing what was produced and how it meets the criteria>

## 3. Implementation Notes
<Free-form description: Methods used, key decisions, assumptions made, tools/frameworks applied, and any deviations from the plan with clear justification. Explain your reasoning transparently.>

## 4. Validation & Self-Check
- Alignment with Plan: [Met / Partially Met / Not Met]
- Success Criteria Met: [Yes / No / Partially]
- Known Limitations: <Missing data, scope trade-offs, or unresolved constraints>
- Ready for Review: [Yes / No] <If No, explicitly state what blocks handoff>

## 5. Artifacts & References
- Deliverable: list of artefacts with links to them.
- Input sources: <paths/URLs/docs used>
- Standards applied: <Guidelines, frameworks, or conventions followed>

GUIDING PRINCIPLES
- Execute only the assigned step. Do not plan ahead, critique, or modify unrelated components.
- Follow the plan's scope, dependencies, and success criteria exactly. If blocked, document why and output a partial result rather than guessing.
- Maintain domain-appropriate quality: rigorous for analysis, clear for writing, structured for data/configs.
- Document all assumptions, tool choices, and deviations transparently. The Critic agent will use this report for evaluation.
- Ensure artifacts are self-contained, well-formatted, and explicitly reference how they satisfy each success criterion.
- Never generate placeholder, incomplete, or speculative deliverables. State exactly what is missing if scope cannot be fully met.

Your execution is the foundation for downstream evaluation. Make it precise, verifiable, and fully aligned with the plan."""

    tool_specification = {
        "type": "function",
        "name": "subagent_worker",
        "description": (
            "An expert AI agent that executes specific, planned steps to produce concrete artifacts "
            "across any domain — documents, analysis, configurations, drafts, datasets, or structured content. "
            "Focuses on precise implementation and deliverable creation without planning or self-evaluation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "A clear rationale for invoking the worker — "
                        "which planned step is being executed and what deliverable it aims to produce."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "A detailed description of the step to be executed, including:\n"
                        "- Step title and exact requirements from the plan\n"
                        "- Expected deliverable: format, scope, and success criteria\n"
                        "- Dependencies: prerequisite artifacts, data, or decisions that must be available\n"
                        "- Constraints: deadlines, quality standards, style guides, or technical limits\n\n"
                        "Recommended additions for higher execution quality:\n"
                        "- Input materials: paths to files, datasets, briefs, or reference docs\n"
                        "- Domain context: field, audience, methodology, or framework to apply\n"
                        "- Tools/Formats: preferred output format or specific tools to use (if applicable)\n\n"
                    ),
                },
            },
            "required": ["reason", "task"],
            "additionalProperties": False,
        },
        "strict": True,
    }

    def __init__(self, task):
        self.task = task

    @classmethod
    async def create_instance(cls, task, **kwargs):
        instance = cls(task)
        return instance

    def get_initial_data(self):
        return self.agent_role, self.task

    def get_agent_name(self):
        return self.agent_name

    @classmethod
    def get_agent_specification(cls):
        return cls.tool_specification
