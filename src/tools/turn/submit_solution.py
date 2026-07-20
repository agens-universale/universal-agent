from typing import Any, Dict, Optional, TYPE_CHECKING
import json
from tools.simple_tool_base import AbstractSimpleTool
from utils.arg_decorators import parse_json_object_argument
from utils.json_arg_parser import JsonArgParseError, parse_json_object_arg

if TYPE_CHECKING:
    from intent_guard.guard import FinishGateDecision


SOLUTION_TYPES = (
    "full_solution",
    "partial_solution",
    "direct_answer",
    "couldnt_solve",
    "need_additional_info",
    "abandoned",
)

class SubmitSolution(AbstractSimpleTool):

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
            "tool": SubmitSolution.registered_tool_name(),
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
    def _parse_handoff_from_arguments(cls, payload: Dict[str, Any]) -> Dict[str, Any] | None:
        handoff_data = payload.get("handoff_data")
        if isinstance(handoff_data, dict):
            return handoff_data
        if "handoff" not in payload:
            return None
        raw = payload.get("handoff")
        if isinstance(raw, dict):
            return raw
        try:
            return parse_json_object_arg(raw, field="handoff")
        except JsonArgParseError:
            return None

    @classmethod
    def output_from_cycle_stop_arguments(cls, tool_arguments: str) -> Optional[str]:
        """
        Build ``function_call_output`` JSON from ``function_call.arguments`` on cycle_stop.

        Best-effort parse without re-running full tool validation (finish gate already ran).
        """
        if not isinstance(tool_arguments, str) or not tool_arguments.strip():
            return None
        try:
            payload = json.loads(tool_arguments)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None

        solution_type = payload.get("solution_type")
        if solution_type is not None and not isinstance(solution_type, str):
            solution_type = str(solution_type)

        task_report = payload.get("task_report")
        if not isinstance(task_report, str):
            task_report = "" if task_report is None else str(task_report)

        comment = payload.get("comment")
        if comment is not None and not isinstance(comment, str):
            comment = str(comment)

        handoff = cls._parse_handoff_from_arguments(payload)

        return cls._result(
            ok=True,
            status="submitted",
            message="Task outcome submitted.",
            task_report=task_report,
            solution_type=solution_type,
            comment=comment if isinstance(comment, str) and comment.strip() else None,
            handoff=handoff,
            error=None,
        )

    @classmethod
    def cycle_stop_output_error(cls, *, detail: str) -> str:
        return cls._result(
            ok=False,
            status="cycle_stop_output_error",
            message=detail,
            task_report="",
            error={"type": "cycle_stop", "message": detail},
        )

    @staticmethod
    def _finish_gate_block_from_validation(validation: Any) -> Dict[str, Any]:
        return {
            "action": validation.action,
            "ok": validation.ok,
            "trace_ok": validation.trace_ok,
            "report_ok": validation.report_ok,
            "is_question": bool(getattr(validation, "is_question", False)),
            "trace_gaps": list(validation.trace_gaps),
            "report_mismatches": list(validation.report_mismatches),
            "missing_requirements": list(validation.missing_requirements),
            "conflicts": list(validation.conflicts),
            "coverage_score": validation.coverage_score,
        }

    @classmethod
    def apply_finish_gate_decision(
        cls,
        tool_output_json: str,
        decision: "FinishGateDecision",
    ) -> str:
        """Patch turn_finish JSON after quality/finish gate (intent_guard)."""
        try:
            payload = json.loads(tool_output_json)
        except json.JSONDecodeError:
            return tool_output_json
        if not isinstance(payload, dict):
            return tool_output_json

        from intent_guard.feedback import build_finish_gate_feedback

        if decision.outcome == "reject" and decision.validation is not None:
            validation = decision.validation
            feedback = (decision.feedback_text or "").strip() or build_finish_gate_feedback(
                validation
            )
            payload["ok"] = False
            payload["status"] = "rejected_by_finish_gate"
            payload["message"] = feedback
            payload["finish_gate"] = cls._finish_gate_block_from_validation(validation)
        elif decision.validation is not None:
            payload["finish_gate"] = cls._finish_gate_block_from_validation(
                decision.validation
            )
            if decision.outcome == "accept":
                payload.setdefault("ok", True)
                payload.setdefault("status", "submitted")
            elif decision.outcome == "fail_open_limit":
                payload["finish_gate"]["runtime_outcome"] = "fail_open_limit"
            elif decision.outcome == "fail_open_error":
                payload["finish_gate"]["runtime_outcome"] = "fail_open_error"
        elif decision.outcome in ("fail_open_limit", "fail_open_error", "skipped"):
            payload["finish_gate"] = {"runtime_outcome": decision.outcome}

        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
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
                    "task_report": {
                        "type": "string",
                        "description": (
                            "Final human-facing summary for this turn: what was done, result, assumptions, evidence. "
                            "Plain text, no markdown. Not for mid-task status updates."
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
                            "Machine-readable payload for downstream workflows; use \"{}\" when nothing extra is needed. "
                            "Example: '{\"artifacts\":[\"report.md\"]}'."
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
        return '''Tool name: turn_finish
Tool description: Finalizes turn: yes. Signals that the CURRENT USER TURN is finished and the outcome is ready to hand back to the user.
IMPORTANT: This marks end of the current turn, NOT end of the whole thread/session.
Use solution_type to classify the turn outcome; task_report for what was done in this turn; comment for caveats or next steps.

Input schema:
{
  "solution_type": "full_solution" | "partial_solution" | "direct_answer" | "couldnt_solve" | "need_additional_info" | "abandoned",
  "task_report": "string (required)",
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

- task_report: What was done in this turn (steps, result, assumptions/evidence). Plain text, brief.
- comment: Optional caveats, risks, unresolved items, or next steps.
- handoff: Required JSON object string (see Handoff format). Minimal valid value: "{}".

Return format (JSON string): {"tool":"turn_finish","ok":true,"status":"...","message":"...","error":null,"solution_type":"...","comment":"..."|null,"handoff":{...},"task_report":"..."}
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
                    "message": (
                        "handoff is required. Must decode to a JSON object; "
                        "pass it as a JSON string (e.g. \"{}\")."
                    ),
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
