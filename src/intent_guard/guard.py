from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from intent_guard.feedback import build_finish_gate_feedback
from intent_guard.final_report_consistency import validate_final_report_vs_user_intent
from intent_guard.validation_result import ValidationResult

_logger = logging.getLogger(__name__)

_DEFAULT_FINISH_GATE_MAIN = 10
_DEFAULT_FINISH_GATE_SUB = 3


def resolve_finish_gate_max_rejections(cfg: dict | None, state: Any) -> int:
    """Макс. число reject за turn/run (§10); всегда >= 1."""
    if not isinstance(cfg, dict):
        cfg = {}
    is_main = getattr(state, "parent_state_id", None) is None
    key = (
        "finish_gate_max_iterations"
        if is_main
        else "subagent_finish_gate_max_iterations"
    )
    default = _DEFAULT_FINISH_GATE_MAIN if is_main else _DEFAULT_FINISH_GATE_SUB
    raw = cfg.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _logger.warning(
            "finish_gate_reject_limit_invalid %s=%r is not int; using %s",
            key,
            raw,
            default,
        )
        return default
    if value < 1:
        _logger.warning(
            "finish_gate_reject_limit_invalid %s=%s must be >= 1; using %s",
            key,
            value,
            default,
        )
        return default
    return value


@dataclass(frozen=True)
class FinishGateDecision:
    outcome: Literal[
        "accept",
        "reject",
        "fail_open_limit",
        "fail_open_error",
        "skipped",
    ]
    validation: ValidationResult | None = None
    feedback_text: str = ""


class FinishGateGuard:
    """Оркестратор finish gate на один (ctx, state) — см. SPEC §5.6."""

    def __init__(
        self,
        *,
        ctx: Any,
        state: Any,
        logger: Any | None = None,
    ) -> None:
        self._ctx = ctx
        self._state = state
        self._logger = logger if logger is not None else _logger

    @property
    def enabled(self) -> bool:
        cfg = getattr(self._ctx, "config_dict", {}) or {}
        if not isinstance(cfg, dict):
            return False
        strict = bool(cfg.get("strict_submit_solution_validation", True))
        backend = str(cfg.get("backend_api_mode") or "").strip().lower()
        return strict and backend == "chat_completions"

    def evaluate_submit_solution(
        self,
        *,
        submit_solution_arguments: Any,
    ) -> FinishGateDecision:
        if not self.enabled:
            return FinishGateDecision(outcome="skipped")

        cfg = getattr(self._ctx, "config_dict", {}) or {}
        if not isinstance(cfg, dict):
            cfg = {}

        max_r = resolve_finish_gate_max_rejections(cfg, self._state)
        count_before = int(getattr(self._state, "finish_gate_rejection_count", 0) or 0)
        parent_state_id = getattr(self._state, "parent_state_id", None)
        agent_kind = "subagent" if parent_state_id is not None else "main"

        if count_before >= max_r:
            self._logger.warning(
                "finish_gate_iteration_limit_reached finish_gate_rejection_count=%s "
                "max_rejections=%s agent_kind=%s parent_state_id=%s",
                count_before,
                max_r,
                agent_kind,
                parent_state_id,
            )
            return FinishGateDecision(outcome="fail_open_limit")

        active_user_messages = list(
            getattr(self._state, "current_user_messages", []) or []
        )
        full_user_messages_log = list(
            getattr(self._state, "user_messages_log", []) or []
        )
        if not active_user_messages:
            active_user_messages = full_user_messages_log
        active_set = set(active_user_messages)
        context_user_messages = [
            msg for msg in full_user_messages_log if msg not in active_set
        ]

        try:
            validation = validate_final_report_vs_user_intent(
                ctx=self._ctx,
                state=self._state,
                current_user_requests=active_user_messages,
                context_user_messages=context_user_messages,
                submit_solution_arguments=submit_solution_arguments,
            )
        except Exception as exc:
            self._logger.warning(
                "finish_gate_validation_failed; allow submit_solution "
                "to avoid inconsistent tool_call history: %s",
                exc,
                exc_info=True,
            )
            return FinishGateDecision(outcome="fail_open_error")

        if validation.ok:
            return FinishGateDecision(outcome="accept", validation=validation)

        self._state.finish_gate_rejection_count = count_before + 1
        feedback_text = build_finish_gate_feedback(validation)
        self._logger.warning(
            "finish_gate_rejected finish_gate_rejection_count=%s/%s agent_kind=%s "
            "parent_state_id=%s action=%s trace_ok=%s report_ok=%s "
            "report_mismatches=%s trace_gaps=%s",
            self._state.finish_gate_rejection_count,
            max_r,
            agent_kind,
            parent_state_id,
            validation.action,
            validation.trace_ok,
            validation.report_ok,
            validation.report_mismatches,
            validation.trace_gaps,
        )
        return FinishGateDecision(
            outcome="reject",
            validation=validation,
            feedback_text=feedback_text,
        )
