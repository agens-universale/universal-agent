from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from intent_log import deserialize_list, records_to_jsonl_lines
from intent_guard.trace_digest import build_finish_gate_trace_digest
from intent_guard.agent_logging import (
    log_finish_gate_decision,
    log_finish_gate_skipped,
    log_finish_gate_validate_done,
    log_finish_gate_validate_start,
)
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
    """Оркестратор finish gate (quality gate) на один (ctx, state) — см. SPEC §5.6."""

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
        strict = bool(cfg.get("strict_turn_finish_validation", True))
        backend = str(cfg.get("backend_api_mode") or "").strip().lower()
        return strict and backend == "chat_completions"

    def evaluate_turn_finish(
        self,
        *,
        finish_handoff_arguments: Any,
    ) -> FinishGateDecision:
        """Run quality gate on the agent's finish-turn handoff (arguments JSON)."""
        cfg = getattr(self._ctx, "config_dict", {}) or {}
        if not isinstance(cfg, dict):
            cfg = {}
        backend = str(cfg.get("backend_api_mode") or "").strip().lower()
        parent_state_id = getattr(self._state, "parent_state_id", None)
        agent_kind = "subagent" if parent_state_id is not None else "main"

        if not self.enabled:
            reason = "disabled"
            if not bool(cfg.get("strict_turn_finish_validation", True)):
                reason = "strict_turn_finish_validation=false"
            elif backend != "chat_completions":
                reason = f"backend_api_mode={backend}"
            log_finish_gate_skipped(
                self._logger,
                reason=reason,
                backend_api_mode=backend,
            )
            decision = FinishGateDecision(outcome="skipped")
            log_finish_gate_decision(
                self._logger,
                outcome=decision.outcome,
                agent_kind=agent_kind,
            )
            return decision

        max_r = resolve_finish_gate_max_rejections(cfg, self._state)
        count_before = int(getattr(self._state, "finish_gate_rejection_count", 0) or 0)

        if count_before >= max_r:
            self._logger.warning(
                "finish_gate_iteration_limit_reached finish_gate_rejection_count=%s "
                "max_rejections=%s agent_kind=%s parent_state_id=%s",
                count_before,
                max_r,
                agent_kind,
                parent_state_id,
            )
            decision = FinishGateDecision(outcome="fail_open_limit")
            log_finish_gate_decision(
                self._logger,
                outcome=decision.outcome,
                agent_kind=agent_kind,
                rejection_count=count_before,
                max_rejections=max_r,
            )
            return decision

        current_user_intent = records_to_jsonl_lines(
            deserialize_list(getattr(self._state, "current_user_intent", []) or [])
        )
        user_intent_log = records_to_jsonl_lines(
            deserialize_list(getattr(self._state, "user_intent_log", []) or [])
        )

        digest_chars = 0
        try:
            digest = build_finish_gate_trace_digest(state=self._state, cfg=cfg)
            digest_chars = len(digest.trace_digest_text or "")
        except Exception:
            digest_chars = 0

        log_finish_gate_validate_start(
            self._logger,
            agent_kind=agent_kind,
            current_intent_lines=len(current_user_intent),
            user_intent_log_lines=len(user_intent_log),
            rejection_count=count_before,
            max_rejections=max_r,
            digest_chars=digest_chars,
        )

        try:
            validation = validate_final_report_vs_user_intent(
                ctx=self._ctx,
                state=self._state,
                current_user_requests=current_user_intent,
                context_user_messages=user_intent_log,
                finish_handoff_arguments=finish_handoff_arguments,
            )
        except Exception as exc:
            self._logger.warning(
                "finish_gate validator error; fail-open accept so pending finish handoff "
                "still pairs with its tool_call in chat/completions history: %s",
                exc,
                exc_info=True,
            )
            decision = FinishGateDecision(outcome="fail_open_error")
            log_finish_gate_decision(
                self._logger,
                outcome=decision.outcome,
                agent_kind=agent_kind,
                rejection_count=count_before,
                max_rejections=max_r,
            )
            return decision

        log_finish_gate_validate_done(
            self._logger,
            agent_kind=agent_kind,
            validation=validation,
        )

        if validation.ok:
            decision = FinishGateDecision(outcome="accept", validation=validation)
            log_finish_gate_decision(
                self._logger,
                outcome=decision.outcome,
                agent_kind=agent_kind,
                rejection_count=count_before,
                max_rejections=max_r,
                validation=validation,
            )
            return decision

        self._state.finish_gate_rejection_count = count_before + 1
        feedback_text = build_finish_gate_feedback(validation)
        decision = FinishGateDecision(
            outcome="reject",
            validation=validation,
            feedback_text=feedback_text,
        )
        log_finish_gate_decision(
            self._logger,
            outcome=decision.outcome,
            agent_kind=agent_kind,
            rejection_count=self._state.finish_gate_rejection_count,
            max_rejections=max_r,
            validation=validation,
            feedback_preview=feedback_text,
        )
        return decision
