import json
from typing import Any, Dict, Optional

from gc_core.intent_guard.finish_output import apply_finish_gate_decision
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.arg_decorators import parse_json_object_argument

_VALIDATION_FAILED = "Validation failed."

SOLUTION_TYPES = (
    "full_solution",
    "partial_solution",
    "direct_answer",
    "couldnt_solve",
    "need_additional_info",
    "abandoned",
)

TASK_REPORT_MAX_CHARS = 10000


class SubmitSolution(AbstractSimpleTool):
    @staticmethod
    def _result(
        *,
        ok: bool,
        status: str,
        message: str,
        task_report: str,
        task: str | None = None,
        solution_type: str | None = None,
        comment: str | None = None,
        handoff: Dict[str, Any] | None = None,
        error: Dict[str, Any] | None = None,
    ) -> str:
        out: Dict[str, Any] = {
            "tool": SubmitSolution.registered_tool_name(),
            "ok": ok,
            "status": status,
        }
        # Успешный turn_finish — управляющий ack. Содержательные поля уже есть
        # в function_call.arguments: их повтор раздувает trace, digest судьи и
        # intent log. Диагностику сохраняем только для отказного результата.
        if ok:
            return json.dumps(out, ensure_ascii=False)

        out["message"] = message
        out["error"] = error
        if solution_type is not None:
            out["solution_type"] = solution_type
        if comment is not None:
            out["comment"] = comment
        if handoff is not None:
            out["handoff"] = handoff
        out["task"] = task
        out["task_report"] = task_report
        return json.dumps(out, ensure_ascii=False)

    @staticmethod
    def apply_finish_gate_decision(tool_output_json, decision):
        return apply_finish_gate_decision(tool_output_json, decision)

    @classmethod
    def tool_description(
        cls, config_dict: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "turn_finish",
            "description": (
                "Finalizes turn: yes. "
                "Call exactly once when you end the current user turn (not the whole thread/session). "
                "Use for done / partial / blocked outcomes with a self-contained report "
                "and machine-readable handoff. "
                "Do NOT use for brainstorming, mid-task status, or progress while work continues. "
                "Call only after reasonable work and verification for this turn when needed. "
                "Do not call early just to return control."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "solution_type": {
                        "type": "string",
                        "description": (
                            "Turn outcome: full_solution (objective done), partial_solution (meaningful progress, not done), "
                            "direct_answer (direct answer/explanation completed this turn), couldnt_solve, need_additional_info (blocked), "
                            "abandoned."
                        ),
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "What the user asked this turn, in 1–2 phrases, in the user's terms. "
                            "Not what was done — that is task_report."
                        ),
                    },
                    "task_report": {
                        "type": "string",
                        "description": (
                            "Final human-facing summary for this turn: what was done, result, assumptions, evidence. "
                            f"Plain text, no markdown. Max {TASK_REPORT_MAX_CHARS} characters. "
                            "Not for mid-task status updates."
                        ),
                    },
                    "comment": {
                        "type": "string",
                        "description": (
                            "Caveats, risks, unresolved items, or suggested next steps for the user."
                        ),
                    },
                    "handoff": {
                        "type": "string",
                        "description": (
                            "Must decode to a JSON object (dictionary), not an array, scalar, or null. "
                            "Pass the object as one JSON string (this field is string-typed, not a nested object). "
                            'Machine-readable payload for downstream workflows; use "{}" when nothing extra is needed. '
                            'Example: \'{"artifacts":["report.md"]}\'.'
                        ),
                    },
                },
                "required": [
                    "solution_type",
                    "task",
                    "task_report",
                    "handoff",
                ],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(
        cls, config_dict: Optional[Dict[str, Any]] = None
    ) -> str:
        return (
            """Tool name: turn_finish
Tool description: Finalizes turn: yes. Signals that the CURRENT USER TURN is finished and the outcome is ready to hand back to the user.
IMPORTANT: This marks end of the current turn, NOT end of the whole thread/session.
Use solution_type to classify the turn outcome; task for what the user asked this turn; task_report for what was done in this turn; comment for caveats or next steps.

Input schema:
{
  "solution_type": "full_solution" | "partial_solution" | "direct_answer" | "couldnt_solve" | "need_additional_info" | "abandoned",
  "task": "string (required)",
  "task_report": "string (required, max """
            + str(TASK_REPORT_MAX_CHARS)
            + """ chars)",
  "comment": "string (optional)",
  "handoff": "{}" (required; JSON object encoded as a string)
}

Handoff format:
- handoff is always a string whose content is valid JSON that parses to an object.
- Use "{}" when the turn has no machine-readable extras.
- Do not pass a nested JSON object in the tool call; serialize the object to a string first.
- Use standard JSON quoting; escape inner double quotes correctly.

Handoff policy:
- turn_finish does not enforce a specific handoff schema.
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
- If this turn benefits from machine-readable transfer, include structured keys in handoff; otherwise use "{}".

Choosing solution_type (for current turn):
- full_solution: turn objective fully completed.
- partial_solution: meaningful partial progress, but objective not fully completed.
- direct_answer: user asked for a direct answer/explanation and it is complete this turn.
- need_additional_info: blocked by missing user input/context.
- couldnt_solve: attempted but could not solve with available means.
- abandoned: stopped by explicit choice/instruction.

- task: What the user asked this turn, in 1–2 phrases, in the user's terms. Not what was done — that is task_report.
- task_report: What was done in this turn (steps, result, assumptions/evidence). Plain text, brief. Max """
            + str(TASK_REPORT_MAX_CHARS)
            + """ characters.
- comment: Optional caveats, risks, unresolved items, or next steps.
- handoff: Required JSON object string (see Handoff format). Minimal valid value: "{}".

Success return format (compact JSON ack): {"tool":"turn_finish","ok":true,"status":"submitted"}
Validation failure return format: {"tool":"turn_finish","ok":false,"status":"rejected","message":"...","error":{...},"task_report":""}
"""
        )

    async def acall(
        self,
        solution_type: str,
        task_report: str,
        task: str | None = None,
        comment: str | None = None,
        handoff: str | None = None,
        handoff_data: Dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        return self._submit(
            solution_type=solution_type,
            task=task,
            task_report=task_report,
            comment=comment,
            handoff=handoff,
            handoff_data=handoff_data,
            **kwargs,
        )

    @parse_json_object_argument(
        source_arg="handoff", target_arg="handoff_data"
    )
    def _submit(
        self,
        solution_type: str,
        task_report: str,
        task: str | None = None,
        comment: str | None = None,
        **kwargs: Any,
    ) -> str:
        # Validate solution_type
        if not isinstance(solution_type, str):
            return self._result(
                ok=False,
                status="rejected",
                message=_VALIDATION_FAILED,
                task=None,
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
                message=_VALIDATION_FAILED,
                task=None,
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
                message=_VALIDATION_FAILED,
                task=None,
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
                message=_VALIDATION_FAILED,
                task=None,
                task_report="",
                error={
                    "type": "invalid_argument",
                    "field": "task_report",
                    "message": "task_report cannot be empty or whitespace only.",
                },
            )
        report_len = len(task_report.strip())
        if report_len > TASK_REPORT_MAX_CHARS:
            return self._result(
                ok=False,
                status="rejected",
                message=_VALIDATION_FAILED,
                task=None,
                task_report="",
                error={
                    "type": "limit_exceeded",
                    "field": "task_report",
                    "message": (
                        f"task_report exceeds {TASK_REPORT_MAX_CHARS} chars, got {report_len}"
                    ),
                },
            )

        handoff_data = kwargs.get("handoff_data")
        if not isinstance(handoff_data, dict):
            return self._result(
                ok=False,
                status="rejected",
                message=_VALIDATION_FAILED,
                task=None,
                task_report="",
                error={
                    "type": "invalid_argument",
                    "field": "handoff",
                    "message": (
                        'handoff is required. Must decode to a JSON object; pass it as a JSON string (e.g. "{}").'
                    ),
                },
            )

        # Validate comment (optional)
        if comment is not None:
            if not isinstance(comment, str):
                return self._result(
                    ok=False,
                    status="rejected",
                    message=_VALIDATION_FAILED,
                    task=None,
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
                    message=_VALIDATION_FAILED,
                    task=None,
                    task_report="",
                    error={
                        "type": "limit_exceeded",
                        "field": "comment",
                        "message": "comment is too long.",
                    },
                )

        return self._result(
            ok=True,
            status="submitted",
            message="Task outcome submitted.",
            task=None,
            task_report="",
            solution_type=solution_type,
            comment=None,
            handoff=None,
            error=None,
        )
