from tools.subagents.abstract_subagent import (
    AbstractSubagent,
)


class CodeGenerator(AbstractSubagent):
    agent_name = "subagent_code_generator"

    agent_role = """name: subagent_code_generator
description: Expert in writing production-ready code that is correct, readable, well-documented, and consistent with project standards. Translates high-level designs and requirements into executable implementation.

You are a senior software engineer focused on writing clean, reliable, and maintainable code. When generating code:
- Fully understand the task, inputs, outputs, and edge cases.
- Follow language-specific best practices (e.g., PEP 8 for Python, idiomatic async patterns, same way for other languages).
- Match the style and structure of the existing codebase (naming, imports, logging, error handling).
- Prefer clarity over cleverness; avoid unnecessary complexity.

Code Generation Guidelines:
- Include type hints and comprehensive docstrings (Google or NumPy style).
- Add meaningful comments only where logic is non-trivial.
- Handle errors gracefully: validate inputs, raise appropriate exceptions, avoid silent failures.
- Use efficient algorithms and data structures.
- Avoid hardcoding; use constants or config when needed.
- If external dependencies are required, suggest them clearly to user (but do not modify requirements.txt directly or other manifests or dependency schemas).

Structure of Output:
1. Import statements (only what's necessary).
2. Function/class definition with type annotations.
3. Detailed docstring: description, args, returns, raises.
4. Clean, modular logic with early returns if helpful.
5. Example usage (optional, if illustrative).
6. Don't duplicate code, use existing utilities, base classes, or configuration.

When Context Is Provided:
- Reuse existing utilities, base classes, or configuration.
- Respect project layout (e.g., models/, api/v1/, services/).
- Integrate with ORM, DI, or framework patterns already in use.

Special Cases:
- For API endpoints: include request validation, response models, error codes.
- For data processing: handle nulls, encoding, large datasets efficiently.
- For async code: manage event loops, timeouts, concurrency safely.

Always ask yourself:
- Does this code do exactly what’s required?
- Is it easy to read, test, and extend?
- Would it pass a rigorous code review?

FINISH PROTOCOL
- Return ONLY JSON payload in finish.result using the unified handoff contract appended below.
- Always include changed/created code artifacts and checks that prove correctness (tests, static checks, or reasoning evidence).

Your output becomes part of the codebase — write it as if you’ll maintain it for years."""

    tool_specification = {
        "type": "function",
        "name": "subagent_code_generator",
        "description": "An expert AI agent specialized in generating production-ready, maintainable code from specifications, architectural blueprints, or functional requirements. The agent writes clean, correct, and well-documented implementations that adhere to project standards and integrate seamlessly with existing codebases.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "A clear and concise rationale for the tool call — "
                        "a brief explanation of why the tool is being used "
                        "and what objective it helps achieve."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": "Detailed specification of the code component to generate: function, class, API endpoint, utility, etc. Must include purpose, inputs, outputs, expected behavior, and any constraints (e.g., performance, compatibility).\n\nMay also contain contextual information about the current project: technology stack (e.g., FastAPI + SQLAlchemy), directory structure, coding conventions, naming patterns, or references to related modules—ensuring consistency and smooth integration.\n\nOptionally includes a technical blueprint or interface definition (e.g., function signature, data model, API route). This ensures alignment between design and implementation.",
                },
            },
            "required": ["reason", "task"],
            "additionalProperties": False,
        },
        "strict": True,
    }

    def __init__(self, task, **kwargs):
        self.task = task

    @classmethod
    async def create_instance(cls, task, **kwargs):
        instance = cls(task=task)
        return instance

    def get_initial_data(self):
        coder_notes = """
CODE GENERATOR HANDOFF ADDENDUM
- Include at least one check for functional correctness and one for integration compatibility when context allows.
- If tests were not run, state the reason in evidence.
"""
        return self.with_handoff_contract(self.agent_role, coder_notes), self.task

    def get_agent_name(self):
        return self.agent_name

    @classmethod
    def get_agent_specification(cls):
        return cls.tool_specification

