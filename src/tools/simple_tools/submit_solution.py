import sys
from typing import Dict, Any
from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool
import builtins


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
            "description": "Signals that the AI agent has completed all work and is ready to submit the final solution. This tool must be called exactly upon full task completion, with a concise yet comprehensive summary of actions taken and results achieved. ALWAYS call this tool on task final answer and finish",
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


def test_submit_solution_basic():
    """Test successful submission with valid task_report."""
    tool = SubmitSolution()
    result = tool(task_report="Added login endpoint and validated credentials. Tests pass.")

    assert isinstance(result, dict), "Result must be a dictionary"
    assert result["status"] == "submitted", f"Expected 'submitted', got: {result['status']}"
    assert "message" in result and "completed" in result["message"]
    assert result["task_report"] == "Added login endpoint and validated credentials. Tests pass."

    print("test_submit_solution_basic passed")


def test_submit_solution_missing_task_report():
    """Ensure TypeError or ValueError is raised if task_report is missing."""
    tool = SubmitSolution()

    try:
        tool()  # no args
        assert False, "Should raise TypeError for missing required argument"
    except (TypeError, ValueError):
        pass  # Expected

    print("test_submit_solution_missing_task_report passed")


def test_submit_solution_empty_task_report():
    """Reject empty or whitespace-only task_report."""
    tool = SubmitSolution()

    try:
        tool(task_report="")
        assert False, "Empty task_report should be rejected"
    except ValueError as e:
        assert "empty" in str(e).lower(), f"Wrong error message: {e}"

    try:
        tool(task_report="   \n  \t  ")
        assert False, "Whitespace-only task_report should be rejected"
    except ValueError as e:
        assert "empty" in str(e).lower(), f"Wrong error message: {e}"

    print("test_submit_solution_empty_task_report passed")


def test_submit_solution_non_string_task_report():
    """Reject non-string types for task_report."""
    tool = SubmitSolution()
    invalid_values = [None, 123, [], {}, True]

    for val in invalid_values:
        try:
            tool(task_report=val)
            assert False, f"Should reject task_report={repr(val)}"
        except ValueError as e:
            assert "must be a string" in str(e), f"Unexpected error message: {e}"

    print("test_submit_solution_non_string_task_report passed")


def test_submit_solution_too_long():
    """Reject task_report longer than 1024 characters."""
    tool = SubmitSolution()
    long_report = "x" * 2050

    try:
        tool(task_report=long_report)
        assert False, "Should reject long task_report"
    except ValueError as e:
        assert "under 2048" in str(e) or "too long" in str(e), f"Unexpected error: {e}"

    # Should accept exactly 1024
    valid_report = "x" * 1024
    try:
        result = tool(task_report=valid_report)
        assert result["status"] == "submitted"
        assert len(result["task_report"]) == 1024
    except ValueError:
        assert False, "1024-char report should be accepted"

    print("test_submit_solution_too_long passed")


def test_submit_solution_normalize_text():
    """Test that text normalization works correctly."""
    tool = SubmitSolution()

    dirty = "\r\n  First line.\r\n  Second line.  \n  \r\n"
    cleaned = tool._normalize_text(dirty)

    expected = "  First line.\n  Second line."
    assert cleaned == expected, f"Got: {repr(cleaned)}, expected: {repr(expected)}"

    print("test_submit_solution_normalize_text passed")


def test_submit_solution_prompt_description():
    """Ensure prompt includes correct parameter info."""
    tool = SubmitSolution()
    prompt = tool.prompt_description()

    assert isinstance(prompt, str)
    assert "task_report" in prompt, "Prompt must describe task_report"
    assert "short but full" in prompt.lower()
    assert "maximum length: ~500 characters" in prompt.lower()
    assert "implemented user authentication" in prompt.lower(), "Example missing or incorrect"

    print("test_submit_solution_prompt_description passed")


def test_submit_solution_tool_description_schema():
    """Validate that tool_description matches required JSON schema."""
    tool = SubmitSolution()
    desc = tool.tool_description()

    assert desc["name"] == "submit_solution"
    assert "task_report" in desc["parameters"]["properties"]
    assert desc["parameters"]["required"] == ["task_report"]
    assert desc["parameters"]["additionalProperties"] is False
    assert desc.get("strict") is True

    print("test_submit_solution_tool_description_schema passed")


def test_submit_solution_return_structure():
    """Ensure consistent return structure."""
    tool = SubmitSolution()
    result = tool(task_report="Work completed.")

    expected_keys = {"status", "message", "task_report"}
    assert set(result.keys()) == expected_keys, f"Keys mismatch: {result.keys()}"

    for k in expected_keys:
        assert isinstance(result[k], str), f"{k} must be string"
        assert len(result[k]) > 0, f"{k} must not be empty"

    print("test_submit_solution_return_structure passed")


def run_tests():
    """Run all unit tests."""
    test_submit_solution_basic()
    test_submit_solution_missing_task_report()
    test_submit_solution_empty_task_report()
    test_submit_solution_non_string_task_report()
    test_submit_solution_too_long()
    test_submit_solution_normalize_text()
    test_submit_solution_prompt_description()
    test_submit_solution_tool_description_schema()
    test_submit_solution_return_structure()

    print("\nAll tests for submit_solution passed successfully.")


if __name__ == "__main__":
    run_tests()

    # Example manual usage:
    # tool = SubmitSolution()
    # result = tool(task_report="Refactored auth module. Introduced token refresh. Fixed logout race condition. All tests green.")
    # print("Submission result:", result)