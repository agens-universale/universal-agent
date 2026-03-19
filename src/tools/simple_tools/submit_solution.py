from typing import Dict, Any
from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool


class SubmitSolution(AbstractSimpleTool):
    """
    Tool that signals the AI agent has completed the task and is ready to submit the final solution.
    Requires a concise final report summarizing key steps and the outcome or direct answer to users's question.
    This should be called only when all work is verified complete.
    """

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "submit_solution",
            "description": (
                "Signals that the AI agent has completed all work and is ready to submit the final solution. "
                "This tool must be called exactly upon full task completion, with a concise yet comprehensive "
                "summary of actions taken and results achieved. ALWAYS call this tool for the final user response. "
                "(The separate answer_question tool is not available; use submit_solution instead.)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_report": {
                        "type": "string",
                        "description": "A brief but complete summary of the task solution, including key steps performed, the final outcome, and any relevant assumptions. Must be clear, self-contained, and written in plain text (no markdown or code). Keep under 10 sentences. If user asked question and you can make full answer - use this tool."
                    }
                },
                "required": ["task_report"],
                "additionalProperties": False
            },
            "strict": True
        }

    @classmethod
    def prompt_description(cls) -> str:
        description = '''Tool name: submit_solution  
Tool description: Signals that the current task has been fully completed and the solution is ready for submission.
A concise `task_report` must be provided, summarizing:
- Key actions taken
- Final result or implementation
- Any assumptions made

Input schema: {"type": "object", "properties": {"task_report": {"type": "string", "description": "Short final report about task solution - performed steps and final result. Few theses - short but full."}}, "required": ["task_report"], "additionalProperties": false}

Usage:
- Use this tool **only** when you are 100% certain the task is complete.
- This is the final user response mechanism. Do not use answer_question (not available).
- The `task_report` must be brief (3–6 sentences), clear, and self-contained.
- Include enough context so a human can understand what was done without reviewing code.
- Do **not** use markdown, code blocks, or formatting in `task_report`.
- Avoid vague statements like "I fixed it" — explain *what* was fixed and *how*.

Important Rules:
- Never call this tool unless all requirements are satisfied.
- Never call it mid-task or before verification.
- `task_report` must not be empty or generic.
- Maximum length: ~500 characters. Be concise.
- After calling, the session will typically terminate.

Example usage:
{
  "task_report": "Implemented user authentication using JWT tokens. Created /login and /profile endpoints in file /home/dev/project_1/src/logging_controllers.py. Validated input and added error handling. All tests pass."
}
'''
        return description

    def __call__(self, task_report: str, **kwargs) -> Dict[str, str]:
        """
        Execute the submit_solution action with a final report.

        Args:
            task_report (str): A concise summary of actions taken and the final result.

        Returns:
            Dict[str, str]: Status and message indicating submission result.
        """
        # Input validation
        if not isinstance(task_report, str):
            return f"Error: Argument 'task_report' must be a string. Got: {type(task_report)} ({repr(task_report)})"
        if not task_report.strip():
            return "Error: Argument 'task_report' cannot be empty or whitespace only."
        if len(task_report.strip()) > 10000:
            return "Error: task_report is too long. Keep shorter. Make under 10 sentences."

        # Normalize line endings and clean whitespace
        task_report = self._normalize_text(task_report)

        return {
            "status": "submitted",
            "message": "Task completed and solution submitted.",
            "task_report": task_report
        }

    def _normalize_text(self, text: str) -> str:
        """Normalize line endings and trim extra whitespace."""
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        lines = [line.rstrip() for line in text.split('\n')]
        # Remove leading/trailing empty lines
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return '\n'.join(lines)