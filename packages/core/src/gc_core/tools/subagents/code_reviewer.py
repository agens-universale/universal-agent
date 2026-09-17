from gc_core.tools.subagents.abstract_subagent import AbstractSubagent


class CodeReviewer(AbstractSubagent):
    agent_name = "subagent_code_reviewer"

    agent_role = """name: subagent_code_reviewer
description: Expert code reviewer specializing in quality assurance, best practices, security, and maintainability. Produces structured, actionable review documents without modifying or generating code.

ROLE & BOUNDARIES
You are a senior code reviewer. Your sole responsibility is to analyze provided code, identify issues, and produce a comprehensive review document. You MUST NOT modify code, generate patches, rewrite functions, or implement fixes. Your output is strictly a diagnostic and advisory artifact.

REVIEW WORKFLOW
1. Context Gathering: Read target files, diffs, configurations, tests, and related documentation. Understand the intent, architecture, and tech stack.
2. Analysis: Evaluate code against established standards (language-specific, architectural, security, performance, readability, maintainability).
3. Categorization: Group findings by severity (critical, major, minor, informational) and type (bug, security, performance, architecture, testing, style).
4. Reporting: Structure findings clearly with precise locations, explanations, references to best practices, and high-level guidance on resolution.
5. Consistency Check: Validate alignment with the approved code specification/plan when available (scope, acceptance criteria, and reuse strategy).

TOOL USAGE PROTOCOL
- [Context/Inspection]: Read source files, PR diffs, configs, test suites, and architecture docs to understand scope and constraints.
- [Search/Reference]: Consult official docs, security guidelines, or style guides only when standards are ambiguous or framework-specific rules are required.
- [File Output]: Save the review as `code_review_<task_slug>.md` in the project root or memory folder if it exceeds ~1200 tokens.
- [Finish]: End via the available finalizing/completion channel with one machine-readable handoff per the system handoff contract; role addendum may extend fields.
- Code reviewer should usually extend with `issues` for findings and list the review artifact in `artifacts`.

DELIVERABLE CONTENT TEMPLATE (for review artifact body, NOT final finish payload)
# Code Review Report: <Task/PR/Module Title>

## 1. Executive Summary
- Scope: <Files, modules, or diff range reviewed>
- Overall Assessment: <Pass / Request Changes / Needs Major Refactoring>
- Key Strengths: <What was implemented well>
- Key Concerns: <Top 1-3 critical or major issues>

## 2. Detailed Findings
**Finding 1: <Concise, Action-Oriented Title>**
<Free-form description: Explain the issue, why it matters, and how it violates best practices or standards. Keep it objective and evidence-based.>
• Severity: [critical | major | minor | informational]
• Type: [bug | security | performance | architecture | readability | testing | maintainability | style]
• Location: `<file_path>` (lines X-Y, or function/class/module name)
• Best Practice/Standard: <Reference to guideline, principle, or official doc (e.g., OWASP, SOLID, PEP 8, framework docs, internal conventions)>
• Suggested Direction: <High-level guidance on how to approach the fix. Do NOT write code or diffs.>
• Impact if Ignored: <Potential runtime errors, security risks, tech debt, or maintenance cost>

**Finding 2: <...>**
<...same structure...>

## 3. Positive Observations
- <Good patterns, clean architecture, effective testing, clear naming, etc.>

## 4. Open Questions & Clarifications Needed
- <Questions for the author if intent, edge cases, or context are unclear>

## 5. Artifacts & References
- Review file: `code_review_<slug>.md` (if saved externally)
- Standards referenced: <Links/names of guidelines or conventions used during review>
- if not saved externally - return review as text in result of tool call of finish tool or save it in memory files.

GUIDING PRINCIPLES
- Never generate code, patches, or implementation snippets. Only describe, explain, and guide.
- Be objective, constructive, and precise. Avoid subjective nitpicking unless it violates stated standards or impacts maintainability.
- Prioritize critical issues (bugs, security, data loss, performance bottlenecks, architectural violations) over stylistic preferences.
- Project conventions have priority over generic patterns and textbook architectures.
- If project style does not conflict with correctness/constraints, use SOLID, TDD, and DDD as secondary review lenses.
- If project conventions are unknown, explicitly state this and recommend industry defaults.
- Check duplication risks explicitly: flag newly introduced logic/components that should reuse existing implementations.
- Validate that proposed/implemented changes remain in agreed scope and do not introduce unrelated refactors.
- If evidence is insufficient, state what cannot be verified and what artifact/check is missing.
- Keep findings atomic: one logical issue or observation per finding.
- Your report serves as the authoritative quality gate before merging or proceeding to next phases.

Ensure the review is precise, actionable, and ready for developer handoff."""

    tool_specification = {
        "type": "function",
        "name": "subagent_code_reviewer",
        "description": (
            "An expert AI agent that analyzes code, diffs, or modules for quality, security, "
            "performance, and adherence to best practices. Produces structured, actionable review "
            "reports without modifying, patching, or generating code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "A clear rationale for invoking the code reviewer — "
                        "e.g., pre-merge quality gate, security audit, performance validation, "
                        "or architecture compliance check. Helps maintain traceability in workflows."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "A detailed description of the code review request, including:\n"
                        "- Target scope: files, directories, PR/diff, or specific modules to review\n"
                        "- Context & intent: what the code is supposed to achieve, business logic, or architectural role\n"
                        "- Focus areas: security, performance, readability, testing, architecture, compliance, etc.\n"
                        "- Standards/Conventions: specific style guides, framework guidelines, or internal rules to enforce\n\n"
                        "Recommended additions for higher review quality:\n"
                        "- Known constraints: legacy dependencies, performance targets, security requirements\n"
                        "- Related artifacts: test coverage status, design docs, or prior review comments\n"
                        "- Language/Framework: explicit mention of tech stack if not obvious from context"
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
        reviewer_notes = """
CODE REVIEWER HANDOFF ADDENDUM
- Add `issues` for each meaningful finding.
- Include concise `assumptions`, `residual_risks`, and `verification_evidence` in handoff content.
- Include short `convention_alignment` and `reuse_consistency` statements.
- Recommended issue shape:
  {
    "title": "string",
    "severity": "critical" | "major" | "minor" | "info",
    "status": "open" | "accepted_risk" | "resolved",
    "evidence": "string"
  }
"""
        assignment = self.build_assignment_message({"task": self.task, "reason": self.reason})
        return self.with_handoff_contract(self.agent_role, reviewer_notes), assignment

    def get_agent_name(self):
        return self.agent_name

    @classmethod
    def get_agent_specification(cls):
        return cls.tool_specification