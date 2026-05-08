from typing import Dict, Any
import json
from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool
from utils.arg_decorators import parse_json_object_argument


SOLUTION_TYPES = (
    "full_solution",
    "partial_solution",
    "answer_question",
    "couldnt_solve",
    "need_additional_info",
    "abandoned",
)

class SubmitSolution(AbstractSimpleTool):
    TERMINAL_TOOL_OUTPUT_MESSAGE = "Dialog turn finished. Waiting for new user questions."

    @staticmethod
    def _result(
        *,
        ok: bool,
        status: str,
        message: str,
        task_report: str,
        solution_type: str | None = None,
        comment: str | None = None,
        handoff: Dict[str, Any] | None = None,
        error: Dict[str, Any] | None = None,
    ) -> str:
        out: Dict[str, Any] = {
            "tool": "submit_solution",
            "ok": ok,
            "status": status,
            "message": message,
            "error": error,
        }
        if solution_type is not None:
            out["solution_type"] = solution_type
        if comment is not None:
            out["comment"] = comment
        if handoff is not None:
            out["handoff"] = handoff
        out["task_report"] = task_report
        return json.dumps(out, ensure_ascii=False)

    @classmethod
    def build_terminal_tool_output(cls) -> str:
        """
        Synthetic tool output to close submit_solution tool_call in chat/completions history.
        """
        payload: Dict[str, Any] = {
            "tool": "submit_solution",
            "ok": True,
            "status": "submitted",
            "message": cls.TERMINAL_TOOL_OUTPUT_MESSAGE,
            "error": None,
            "synthetic": True,
        }
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def tool_output_on_cycle_stop(cls) -> str:
        return cls.build_terminal_tool_output()

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "submit_solution",
            "description": (
                "Signals that the AI agent has finished the CURRENT USER TURN and is ready to hand control "
                "back to the user. This is end-of-turn, not end-of-thread/session. "
                "Use solution_type to indicate completeness for this turn. task_report describes what was done; "
                "comment is for caveats, risks, or next steps. "
                "Call exactly once at the end of each completed turn, and only after the agent has performed "
                "all reasonable work it can do for this turn (including relevant tool use/verification when needed). "
                "Do not call this tool early just to hand back control."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "solution_type": {
                        "type": "string",
                        "description": "Outcome type for the CURRENT TURN: full_solution (turn objective fully done), partial_solution (partially done), answer_question (direct answer to user question), couldnt_solve (failed to solve), need_additional_info (blocked, need more from user), abandoned (stopped by choice).",
                    },
                    "task_report": {
                        "type": "string",
                        "description": "What was done in this turn: key steps, final result, assumptions, and evidence basis if tools were used. Plain text, no markdown. Required for full_solution, partial_solution, answer_question. For failure types: brief explanation of what happened.",
                    },
                    "comment": {
                        "type": "string",
                        "description": "Optional. Caveats, risks, unresolved items, or suggested next steps for the user.",
                    },
                    "handoff": {
                        "type": "string",
                        "description": (
                            "Required machine-readable handoff payload as a JSON object string. "
                            "Must decode to a JSON object (dictionary), not an array/string/number. "
                            "Example: '{\"result\":\"ok\",\"artifacts\":[\"report.md\"]}'."
                        ),
                    },
                },
                "required": ["solution_type", "task_report", "handoff"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls) -> str:
        return '''Tool name: submit_solution
Tool description: Signals that the CURRENT USER TURN is finished and the outcome is ready to hand back to the user.
IMPORTANT: This marks end of the current turn, NOT end of the whole thread/session.
Use solution_type to classify the turn outcome; task_report for what was done in this turn; comment for caveats or next steps.

Input schema:
{
  "solution_type": "full_solution" | "partial_solution" | "answer_question" | "couldnt_solve" | "need_additional_info" | "abandoned",
  "task_report": "string (required)",
  "comment": "string (optional)",
  "handoff": "{\"key\":\"value\"}" (required JSON object string)
}

Handoff policy:
- submit_solution does not enforce a specific handoff schema.
- If a workflow defines a contract (for example, by role/tool prompt), follow that contract.
- Otherwise keep handoff minimal, stable, and machine-readable.

When to call:
- Call exactly once at the end of each completed user turn.
- Do NOT call early. First complete required work for this turn.
- Do NOT call immediately after planning/thinking if the user asked for deeper execution/research.

Readiness checklist before calling:
- Turn objective is completed, or clearly blocked.
- Relevant tools/search/verification for this turn have been performed when needed.
- Key claims in task_report are grounded in actual tool outputs (not assumptions).
- Known limitations/risks are captured in comment when relevant.
- If this turn benefits from machine-readable transfer, include handoff JSON.

Choosing solution_type (for current turn):
- full_solution: turn objective fully completed.
- partial_solution: meaningful partial progress, but objective not fully completed.
- answer_question: user asked for a direct answer/explanation.
- need_additional_info: blocked by missing user input/context.
- couldnt_solve: attempted but could not solve with available means.
- abandoned: stopped by explicit choice/instruction.

- task_report: What was done in this turn (steps, result, assumptions/evidence). Plain text, brief.
- comment: Optional caveats, risks, unresolved items, or next steps.
- handoff: Required structured payload for downstream machine-readable workflows.

Return format (JSON string): {"tool":"submit_solution","ok":true,"status":"...","message":"...","error":null,"solution_type":"...","comment":"..."|null,"handoff":{...},"task_report":"..."}
'''

    def __call__(
        self,
        solution_type: str,
        task_report: str,
        comment: str | None = None,
        handoff: str | None = None,
        handoff_data: Dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        return self._submit(
            solution_type=solution_type,
            task_report=task_report,
            comment=comment,
            handoff=handoff,
            handoff_data=handoff_data,
            **kwargs,
        )

    @parse_json_object_argument(source_arg="handoff", target_arg="handoff_data")
    def _submit(
        self,
        solution_type: str,
        task_report: str,
        comment: str | None = None,
        **kwargs: Any,
    ) -> str:
        # Validate solution_type
        if not isinstance(solution_type, str):
            return self._result(
                ok=False,
                status="rejected",
                message="Validation failed.",
                task_report="",
                error={
                    "type": "invalid_argument",
                    "field": "solution_type",
                    "message": "solution_type must be a string.",
                },
            )
        if solution_type not in SOLUTION_TYPES:
            return self._result(
                ok=False,
                status="rejected",
                message="Validation failed.",
                task_report="",
                error={
                    "type": "invalid_argument",
                    "field": "solution_type",
                    "message": f"solution_type must be one of: {', '.join(SOLUTION_TYPES)}",
                },
            )

        # Validate task_report
        if not isinstance(task_report, str):
            return self._result(
                ok=False,
                status="rejected",
                message="Validation failed.",
                task_report="",
                error={
                    "type": "invalid_argument",
                    "field": "task_report",
                    "message": f"task_report must be a string. Got: {type(task_report)}",
                },
            )
        if not task_report.strip():
            return self._result(
                ok=False,
                status="rejected",
                message="Validation failed.",
                task_report="",
                error={
                    "type": "invalid_argument",
                    "field": "task_report",
                    "message": "task_report cannot be empty or whitespace only.",
                },
            )
        if len(task_report.strip()) > 10000:
            return self._result(
                ok=False,
                status="rejected",
                message="Validation failed.",
                task_report="",
                error={
                    "type": "limit_exceeded",
                    "field": "task_report",
                    "message": "task_report is too long. Keep under 10 sentences.",
                },
            )

        handoff_data = kwargs.get("handoff_data")
        if not isinstance(handoff_data, dict):
            return self._result(
                ok=False,
                status="rejected",
                message="Validation failed.",
                task_report="",
                error={
                    "type": "invalid_argument",
                    "field": "handoff",
                    "message": "handoff is required and must be a JSON object string.",
                },
            )

        # Validate comment (optional)
        if comment is not None:
            if not isinstance(comment, str):
                return self._result(
                    ok=False,
                    status="rejected",
                    message="Validation failed.",
                    task_report="",
                    error={
                        "type": "invalid_argument",
                        "field": "comment",
                        "message": "comment must be a string.",
                    },
                )
            if len(comment.strip()) > 5000:
                return self._result(
                    ok=False,
                    status="rejected",
                    message="Validation failed.",
                    task_report="",
                    error={
                        "type": "limit_exceeded",
                        "field": "comment",
                        "message": "comment is too long.",
                    },
                )

        task_report_norm = self._normalize_text(task_report)
        comment_norm = self._normalize_text(comment) if comment and comment.strip() else None

        return self._result(
            ok=True,
            status="submitted",
            message="Task outcome submitted.",
            task_report=task_report_norm,
            solution_type=solution_type,
            comment=comment_norm,
            handoff=handoff_data,
            error=None,
        )

    def _normalize_text(self, text: str) -> str:
        """Normalize line endings and trim extra whitespace."""
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        lines = [line.rstrip() for line in text.split('\n')]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return '\n'.join(lines)
