from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from gc_core.intent_guard.guard import FinishGateDecision


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


def apply_finish_gate_decision(
    tool_output_json: str,
    decision: FinishGateDecision,
) -> str:
    """Patch turn_finish JSON after quality/finish gate (intent_guard)."""
    try:
        payload = json.loads(tool_output_json)
    except json.JSONDecodeError:
        return tool_output_json
    if not isinstance(payload, dict):
        return tool_output_json

    from gc_core.intent_guard.feedback import build_finish_gate_feedback

    if decision.outcome == "reject" and decision.validation is not None:
        validation = decision.validation
        feedback = (
            decision.feedback_text or ""
        ).strip() or build_finish_gate_feedback(validation)
        payload["ok"] = False
        payload["status"] = "rejected_by_finish_gate"
        payload["message"] = feedback
        payload["finish_gate"] = _finish_gate_block_from_validation(validation)
    elif decision.validation is not None:
        payload["finish_gate"] = _finish_gate_block_from_validation(
            decision.validation
        )
        if decision.outcome == "accept":
            payload.setdefault("ok", True)
            payload.setdefault("status", "submitted")
        elif decision.outcome == "fail_open_limit":
            payload["finish_gate"]["runtime_outcome"] = "fail_open_limit"
        elif decision.outcome == "fail_open_error":
            payload["finish_gate"]["runtime_outcome"] = "fail_open_error"
    elif decision.outcome in (
        "fail_open_limit",
        "fail_open_error",
        "skipped",
    ):
        payload["finish_gate"] = {"runtime_outcome": decision.outcome}

    return json.dumps(payload, ensure_ascii=False)
