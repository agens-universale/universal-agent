from __future__ import annotations

import json
import logging
from typing import Any

from gc_core.intent_guard.validation_result import ValidationResult

_logger = logging.getLogger(__name__)


def validation_result_to_log_dict(result: ValidationResult | None) -> dict[str, Any]:
    if result is None:
        return {}
    return {
        "ok": result.ok,
        "action": result.action,
        "trace_ok": result.trace_ok,
        "report_ok": result.report_ok,
        "is_question": result.is_question,
        "trace_gaps": list(result.trace_gaps),
        "report_mismatches": list(result.report_mismatches),
        "missing_requirements": list(result.missing_requirements),
        "conflicts": list(result.conflicts),
        "coverage_score": result.coverage_score,
    }


def _emit(logger: Any, level: int, event: str, **fields: Any) -> None:
    log = logger if logger is not None else _logger
    if not log.isEnabledFor(level):
        return
    payload = json.dumps(fields, ensure_ascii=False, default=str)
    log.log(level, "finish_gate event=%s %s", event, payload)


def log_finish_gate_skipped(logger: Any, *, reason: str, backend_api_mode: str) -> None:
    _emit(
        logger,
        logging.INFO,
        "skipped",
        quality_gate="finish_gate",
        reason=reason,
        backend_api_mode=backend_api_mode,
    )


def log_finish_gate_validate_start(
    logger: Any,
    *,
    agent_kind: str,
    current_intent_lines: int,
    user_intent_log_lines: int,
    rejection_count: int,
    max_rejections: int,
    digest_chars: int,
) -> None:
    _emit(
        logger,
        logging.INFO,
        "validate_start",
        quality_gate="finish_gate",
        agent_kind=agent_kind,
        current_intent_lines=current_intent_lines,
        user_intent_log_lines=user_intent_log_lines,
        finish_gate_rejection_count=rejection_count,
        max_rejections=max_rejections,
        trace_digest_chars=digest_chars,
    )


def log_finish_gate_validate_done(
    logger: Any,
    *,
    agent_kind: str,
    validation: ValidationResult,
    llm_raw_chars: int | None = None,
) -> None:
    fields: dict[str, Any] = {
        "quality_gate": "finish_gate",
        "agent_kind": agent_kind,
        "validation": validation_result_to_log_dict(validation),
    }
    if llm_raw_chars is not None:
        fields["llm_response_chars"] = llm_raw_chars
    _emit(logger, logging.INFO, "validate_done", **fields)


def log_finish_gate_decision(
    logger: Any,
    *,
    outcome: str,
    agent_kind: str,
    rejection_count: int | None = None,
    max_rejections: int | None = None,
    validation: ValidationResult | None = None,
    feedback_preview: str = "",
) -> None:
    fields: dict[str, Any] = {
        "quality_gate": "finish_gate",
        "outcome": outcome,
        "agent_kind": agent_kind,
    }
    if rejection_count is not None:
        fields["finish_gate_rejection_count"] = rejection_count
    if max_rejections is not None:
        fields["max_rejections"] = max_rejections
    if validation is not None:
        fields["validation"] = validation_result_to_log_dict(validation)
    if feedback_preview:
        fields["feedback_preview"] = feedback_preview[:500]
    level = logging.WARNING if outcome == "reject" else logging.INFO
    _emit(logger, level, "decision", **fields)


def log_finish_gate_output_patched(
    logger: Any,
    *,
    call_id: str,
    outcome: str,
    output_ok: bool | None,
    output_status: str | None,
) -> None:
    _emit(
        logger,
        logging.INFO,
        "finish_turn_output_patched",
        quality_gate="finish_gate",
        call_id=call_id,
        outcome=outcome,
        output_ok=output_ok,
        output_status=output_status,
    )
