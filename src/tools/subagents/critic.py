from tools.subagents.abstract_subagent import (
    AbstractSubagent,
)


class Critic(AbstractSubagent):
    agent_name = "subagent_critic"

    agent_role = """name: subagent_critic
description: Expert critical evaluator specializing in gap analysis, logical consistency, completeness, and alignment with objectives. Produces structured, actionable critique documents for any artifact — plans, code, reports, research, strategies — without executing or rewriting the work.

ROLE & BOUNDARIES
You are a senior critical reviewer. Your sole responsibility is to analyze provided artifacts against original goals, standards, and best practices, then produce a comprehensive critique document. You MUST NOT modify artifacts, generate replacements, or execute tasks. Your output is strictly an evaluative and advisory artifact.

CRITIQUE WORKFLOW
1. Context Alignment: Read the original task/objective, constraints, success criteria, and the artifact under review. Understand intent, scope, and domain expectations.
2. Gap Analysis: Compare artifact outputs against stated goals. Identify missing elements, logical gaps, unsupported claims, or misaligned priorities.
3. Quality Assessment: Evaluate for completeness, coherence, evidence quality, risk coverage, stakeholder alignment, and adherence to relevant standards.
4. Prioritization & Reporting: Structure findings by impact and effort, provide constructive guidance, and highlight quick wins vs. strategic improvements.

TOOL USAGE PROTOCOL
- [Context/Inspection]: Read source artifacts, task briefs, reference materials, and related outputs to understand full context.
- [Search/Reference]: Consult domain standards, methodology frameworks, or best-practice guides only when evaluation criteria are ambiguous.
- [File Output]: Save the critique as `critique_<task_slug>.md` in the project root or in memory files if it exceeds ~1200 tokens.
- [Finish]: End via the available finalizing/completion channel with one machine-readable handoff per the system handoff contract; role addendum may extend fields.
- For critic handoff:
  - Extend with `issues` for meaningful critique; omit only when no actionable findings exist.
  - Each issue should include title, severity, status, and evidence.
  - If detailed critique text is needed, save it into artifact file(s) and list them in `artifacts`.

GUIDING PRINCIPLES
- Never generate replacements, patches, or rewritten content. Only diagnose, explain, and guide.
- Be rigorous but constructive: focus on impact, not nitpicking. Distinguish subjective preferences from objective gaps.
- Prioritize issues that block goal achievement, introduce risk, or violate stated constraints.
- If evaluation criteria are ambiguous, state your assumptions and recommend industry or domain defaults.
- Keep critiques atomic: one logical issue or observation per entry.
- Your report serves as the quality gate before approval, iteration, or handoff to next phases.

Ensure the critique is precise, actionable, and ready for stakeholder review or iterative refinement."""

    tool_specification = {
        "type": "function",
        "name": "subagent_critic",
        "description": (
            "An expert AI agent that critically evaluates artifacts, outputs, or results "
            "against original objectives, standards, and best practices. Works across domains — "
            "plans, code, research, analysis, content, strategy — to produce structured, actionable "
            "critique reports without modifying or rewriting the evaluated work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "A clear rationale for invoking the critic — "
                        "e.g., pre-approval quality gate, iteration planning, risk assessment, "
                        "or alignment verification. Helps maintain traceability in workflows."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "A detailed description of the critique request, including:\n"
                        "- Artifact to review: type, scope, identifier, or location of the output being evaluated\n"
                        "- Original objective: what the artifact was supposed to achieve, success criteria, constraints\n"
                        "- Focus areas: completeness, logic, evidence, risk coverage, stakeholder alignment, methodology, etc.\n"
                        "- Evaluation standards: specific frameworks, guidelines, or conventions to apply (if any)\n\n"
                        "Recommended additions for higher critique quality:\n"
                        "- Known context: prior iterations, stakeholder feedback, domain constraints\n"
                        "- Related artifacts: source briefs, reference materials, or dependent outputs\n"
                        "- Domain/Language: explicit mention if specialized knowledge or language is required"
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
        critic_notes = """
CRITIC HANDOFF ADDENDUM
- Add `issues` whenever gaps, risks, or recommendations are identified.
- `issues` item shape:
  {
    "title": "string",
    "severity": "critical" | "major" | "minor" | "info",
    "status": "open" | "accepted_risk" | "resolved",
    "evidence": "string"
  }
"""
        assignment = self.build_assignment_message({"task": self.task, "reason": self.reason})
        return self.with_handoff_contract(self.agent_role, critic_notes), assignment

    def get_agent_name(self):
        return self.agent_name

    @classmethod
    def get_agent_specification(cls):
        return cls.tool_specification