import json
from typing import Any, Dict, Optional, Tuple

from gc_core.thread_store import load_thread_meta, resolve_threads_dir, save_thread_meta
from gc_core.tools.simple_tool_base import AbstractSimpleTool


class UpdatePlan(AbstractSimpleTool):
    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "update_plan",
            "description": "Update the current plan by providing a complete, up-to-date list of remaining steps to solve the task. Use only for complex tasks with 3 or more logical steps, after significant progress, and when all remaining steps—including trivial ones—are clearly defined. The new plan fully replaces the previous version.",
            "parameters": {
                "type": "object",
                "properties": {
                    "current_plan": {
                        "type": "string",
                        "description": "Current plan as a numbered list of TODOs (e.g., '1. Do X\n2. Do Y') representing all remaining steps to complete the task. Must include every step, even minor ones, and reflect the latest understanding of the work needed."
                    }
                },
                "required": ["current_plan"],
                "additionalProperties": False
            },
            "strict": True
        }
        return description

    @classmethod
    def prompt_description(cls):
        description = '''Tool name: update_plan
Tool description: Write current plan TODOs. What you should do for solving initial user task.
Input schema: {"type": "object", "properties": {"current_plan": {"type": "string", "description": "Plan as list of TODOs. Steps for solving initial user task."}, "required": ["current_plan"], "additionalProperties": false}

Usage:
- Use ONLY when the task involves 3 or more logical steps.
- Call after significant progress (e.g., completing a step or discovering new complexity).
- Always include ALL remaining steps — do not omit any, even trivial ones.
- Never call in isolation without context — always base the plan on prior reasoning.
- Do not use for simple or atomic tasks (e.g., "fix typo", "rename variable").
- Generated plan will replace previous plan version.

<example>
User: Implement user login with JWT authentication and validation.
Assistant: I'll implement JWT-based login with input validation.
Assistant calls `update_plan`:
1. Create login form UI with email and password fields.
2. Add form validation logic (client-side).
3. Implement API call to authenticate user.
4. Handle JWT storage (localStorage + secure practices).
5. Redirect to dashboard on success; show error otherwise.
6. Write unit tests for authentication flow.
7. Run integration tests and build.

[Later, after implementing form and API]
Assistant calls `update_plan` again:
1. Finalize JWT storage with refresh token handling.
2. Update existing components to check auth state.
3. Write unit tests for authentication flow.
4. Run integration tests and build, fix any issues.
</example>

Example explanation:
The assistant used the `update_plan` with TODOs list because:
1. Adding dark mode is a multi-step feature requiring UI, state management, and styling changes.
2. The user explicitly requested tests and build be run afterward.
3. The assistant inferred that tests and build need to pass by adding "Ensure tests and build succeed" as the final task.

You also can run `update_plan` after finishing some steps and decomposing next steps. 
'''
        return description

    def __call__(self, current_plan: str, **kwargs) -> str:
        if not isinstance(current_plan, str):
            raise ValueError(f"Argument 'current_plan' must be a string. Got: {type(current_plan)} ({current_plan})")

        return 'A new plan has been generated. Use it for your next steps.'
