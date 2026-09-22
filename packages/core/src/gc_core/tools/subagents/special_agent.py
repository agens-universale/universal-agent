from typing import Any, Dict

from gc_core.config_modules.chat_completions import subagent_role_max_tokens_from_ctx
from gc_core.model_router import SUBAGENT_CONSUMER_PREFIX
from gc_core.summarize.llm import (
    arequest_auxiliary_completion_text,
    auxiliary_llm_completion_kwargs,
)
from gc_core.tools.subagents.abstract_subagent import AbstractSubagent

_ROLE_GENERATE_PROMPT = """Write prompt for AI assistant by role name and short role descriptions. Please make prompt as good as possible according with some examples.

<Example 1>
name: senior_python_developer
description: Expert Python developer skilled in writing clean, efficient, maintainable, and well-tested code. Specializes in backend logic, CLI tools, modular libraries, and AI-agent integrations using modern Python standards.

You are a professional Python developer responsible for implementing robust, readable, and testable code. When assigned a task:
- Write idiomatic Python (PEP 8 compliant, type hints, clear naming).
- Prefer simplicity and clarity over cleverness.
- Use standard library effectively; avoid unnecessary dependencies.
- Structure code modularly: separate concerns, use functions/classes appropriately.
- Handle errors gracefully with meaningful messages and proper exception types.
- Support cross-platform behavior (Unix/Windows) when relevant.

Development Process:
1. **Understand the goal**: Clarify input/output, side effects, and integration points.
2. **Design the solution**: Choose appropriate data structures, patterns (e.g., callable classes, context managers), and module layout.
3. **Implement logic**: Write concise, documented code with type hints (`-> ReturnType`) and docstrings (Google or NumPy style).
4. **Add error handling**: Validate inputs, raise descriptive exceptions, avoid silent failures.
5. **Write tests**: Generate unit tests using `unittest` or `pytest` that cover:
   - Happy path
   - Edge cases (empty input, invalid types, boundary values)
   - Error paths
6. **Ensure reusability**: Design functions/modules to be importable and composable.

Code Standards:
- Python version: Assume 3.9+ unless specified.
- Format: PEP 8 (use `black`/`isort` standards implicitly).
- Types: Always include type hints.
- Imports: Absolute or relative as appropriate; avoid circular imports.
- Logging: Use `logging` module instead of `print()` for runtime feedback.
- Performance: Optimize only when necessary; favor readability first.

Testing Guidelines:
- Place tests in `tests/test_<module>.py` or same folder if small project.
- Use `assert` statements for basic checks in simple cases; prefer `pytest.raises()` for exceptions.
- Mock external calls (e.g., API, file I/O) where needed.
- Ensure tests are fast, isolated, and repeatable.

Output Format:
Return only the essential deliverables:
1. Main implementation (`.py` file content with full code).
2. Test file (complete with imports and assertions).
3. Any required config or utility snippets (e.g., `pyproject.toml`, CLI entrypoint).

If multiple files are generated, list them at the end with brief comments:
> **Artifacts created:**
> - `src/my_tool.py`: Core logic for [purpose]
> - `tests/test_my_tool.py`: Unit tests covering [cases]

Guidelines:
- Never return incomplete or placeholder code (e.g., `# TODO`).
- Do not invent external APIs or services unless requested.
- If design decisions are involved (e.g., sync vs async, class vs function), explain briefly.
- Avoid global state; make functions pure when possible.

Your code will be executed and tested automatically — it must work correctly the first time.
Return final result via `finish` tool call with all artifacts included.
</Example 1>

<Example 2>
name: task_code_reviewer
description: Expert in analyzing code changes for quality, correctness, style, and long-term maintainability. Conducts structured reviews to catch bugs early, enforce consistency, and improve overall code health.

You are a meticulous code reviewer focused on constructive, precise, and actionable feedback. When reviewing changes:
- Understand the intent behind the code (use description if provided).
- Examine logic for correctness, edge cases, error handling, and race conditions.
- Assess structure, naming, comments, and readability.
- Verify compliance with language-specific best practices and project conventions.
- Identify potential performance, security, or scalability issues.

Code Review Process:
- Start by summarizing the change in your own words to confirm understanding.
- Check for functional correctness: does the code achieve its goal reliably?
- Evaluate design: is the solution appropriate, modular, and non-repetitive?
- Inspect error handling: are exceptions, failures, and invalid inputs managed gracefully?
- Analyze test coverage implications: are new paths tested? Are mocks appropriate?
- Review security aspects: input validation, injection risks, auth checks, data exposure.
- Enforce consistent style and formatting (align with common linters: PEP8, ESLint, etc.).

Feedback Structure:
1. **Summary**: Brief overview of the change and your general impression.
2. **Strengths**: Acknowledge well-written, clean, or clever parts.
3. **Issues**: List specific problems with clear location references (file + line conceptually), severity (critical/blocker, major, minor), and concise explanation.
4. **Suggestions**: Provide concrete, alternative implementations or improvements where applicable.
5. **Questions**: Ask for clarification if intent or behavior is unclear.
6. **Final Verdict**: Indicate whether the code is ready to merge, needs revisions, or requires deeper redesign.

Guiding Principles:
- Be respectful, objective, and helpful — aim to teach and improve.
- Prioritize critical issues (bugs, security) over stylistic preferences.
- Avoid nitpicking unless it impacts readability or team standards.
- Suggest automated solutions (linters, pre-commit hooks) when relevant.

Your goal is not just to find flaws, but to raise the overall quality and sustainability of the codebase.
Return final report as result of tool call of finish tool
</Example 2>

Create prompt for role: {{role}}.
Role description: {{role_description}}

Write actual role prompt in format as in examples. Don't use tags <Example>, write just prompt text itself.
**No reasoning:** Do not write chain-of-thought, analysis prose, `<think>…</think>`, or meta commentary. Emit only the role prompt text."""


class SpecialAgent(AbstractSubagent):
    agent_name = "subagent_special_agent"

    tool_specification = {
        "type": "function",
        "name": "subagent_special_agent",
        "description": "Creates a specialized AI expert agent tailored to a specific role and task. This tool allows you to dynamically define an expert with a precise skill set and delegate a well-scoped technical or analytical task to them. The agent will act as a knowledgeable consultant, providing implementation-ready solutions, code, or architectural guidance based on the defined role and context.",
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
                "role": {
                    "type": "string",
                    "description": "A concise title describing the expert's domain, such as 'Senior Python Backend Developer', 'React Frontend Engineer', 'DevOps Specialist', or 'Data Scientist'. Should reflect the primary skill set needed to complete the task.",
                },
                "role_description": {
                    "type": "string",
                    "description": "A brief (2–3 sentence) summary of the expert's knowledge and strengths. Include key technologies, frameworks, best practices, and problem-solving style. Example: 'Expert in FastAPI and asynchronous Python development with strong experience in designing scalable REST APIs and integrating PostgreSQL with SQLAlchemy.'",
                },
                "task": {
                    "type": "string",
                    "description": "A clear and detailed description of the specific task or feature to be completed. Define the goal, desired outcome, and any critical requirements or constraints.\n\nHighly recommended: Provide context about the current project state — such as existing architecture, technology stack, relevant code files, or recent changes. This helps ensure the solution is accurate, non-redundant, and seamlessly integrates with the current codebase.",
                },
            },
            "required": ["reason", "role", "role_description", "task"],
            "additionalProperties": False,
        },
        "strict": True,
    }

    def __init__(
        self,
        role: str,
        role_description: str,
        task: str,
        reason: str,
        model: str,
    ):
        self.role = role
        self.role_description = role_description
        self.task = task
        self.reason = reason
        self.model = model
        self.agent_role: str | None = None

    @classmethod
    async def create_instance(
        cls,
        role: str,
        role_description: str,
        task: str,
        model: str,
        reason: str = "",
        config_dict: Dict[str, Any] | None = None,
        **kwargs,
    ):
        # config_dict/runtime_notices_enabled: не используются больше здесь — роль
        # резолвится позже через finalize_role(ctx, state) (у ребёнка есть router/
        # сетевые параметры); принимаются для совместимости единого вызова
        # SubagentRunner._run_internal (тот же create_instance(**extra) для всех субагентов).
        return cls(role, role_description, task, reason, model)

    async def afinalize_role(self, ctx: Any, state: Any) -> None:
        """Нативная форма: LLM-вызов идёт на loop вызывающего, без потока.

        Роль генерируется через aux chat/completions (профиль kwargs +
        strip_think); зовётся runner'ом после сборки child-контекста — нужны
        ``ctx.model_router`` и сетевые параметры для Bearer.
        """
        self.agent_role = await self._agenerate_role(ctx, state)

    def _role_request_kwargs(self, ctx: Any, state: Any) -> dict:
        """Аргументы aux-вызова генерации роли — один источник."""
        prompt = _ROLE_GENERATE_PROMPT.replace("{{role}}", self.role).replace(
            "{{role_description}}", self.role_description
        )
        return {
            "ctx": ctx,
            "state": state,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": subagent_role_max_tokens_from_ctx(ctx),
            "consumer": f"{SUBAGENT_CONSUMER_PREFIX}{self.agent_name}",
            "request_label": "subagent_special_agent",
            # Профиль aux-вызовов — с develop: без него роль шла бы мимо
            # `auxiliary_llm` kwargs, которыми правятся все служебные вызовы.
            "payload_overrides": auxiliary_llm_completion_kwargs(ctx),
        }

    async def _agenerate_role(self, ctx: Any, state: Any) -> str:
        """Роль генерируется aux-вызовом; форма одна — асинхронная."""
        return await arequest_auxiliary_completion_text(**self._role_request_kwargs(ctx, state))

    def get_initial_data(self):
        role_prompt = self.agent_role or ""
        special_notes = """
SPECIAL AGENT HANDOFF ADDENDUM
- Follow the system handoff contract; extend with domain fields (issues, metrics, recommendations) only when useful.
- `blockers` / `assumptions` / `next_actions` are base schema fields — fill them (or `[]`).
- Keep the final payload machine-readable and avoid prose-only final handoff.
"""
        assignment = self.build_assignment_message({"task": self.task, "reason": self.reason})
        return self.with_handoff_contract(role_prompt, special_notes), assignment

    def get_agent_name(self):
        return self.agent_name

    @classmethod
    def get_agent_specification(cls):
        return cls.tool_specification
