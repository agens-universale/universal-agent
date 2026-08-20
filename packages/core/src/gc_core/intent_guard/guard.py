from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from gc_core.intent_log import deserialize_list, records_to_jsonl_lines
from gc_core.intent_guard.trace_digest import build_finish_gate_trace_digest
from gc_core.intent_guard.agent_logging import log_finish_gate_decision, log_finish_gate_skipped, log_finish_gate_validate_done, log_finish_gate_validate_start
from gc_core.intent_guard.feedback import build_finish_gate_feedback
from gc_core.intent_guard.final_report_consistency import avalidate_final_report_vs_user_intent
from gc_core.intent_guard.validation_result import ValidationResult

_logger = logging.getLogger(__name__)

_DEFAULT_FINISH_GATE_MAIN = 10
_DEFAULT_FINISH_GATE_SUB = 3


def judge_available(cfg: dict) -> bool:
    """Доступна ли машинерия судейства (finish-gate validator) — инфраструктурное
    условие: только ``backend_api_mode == "chat_completions"``. БЕЗ пользовательских
    ручек (``strict_turn_finish_validation`` — ручка finish-gate, сюда не входит)."""
    if not isinstance(cfg, dict):
        return False
    return str(cfg.get("backend_api_mode") or "").strip().lower() == "chat_completions"


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


async def _maybe_await(value: Any):
    """Шов подмены терпит обе формы заглушки — см. processing_responses._maybe_await."""
    import inspect

    if inspect.isawaitable(value):
        return await value
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
        if not (strict and judge_available(cfg)):
            return False
        # smart (только TUI-проекция): без вызовов инструментов за ход судью не зовём.
        # CLI/server ключа finish_gate_smart не ставят → ветка не срабатывает.
        if cfg.get("finish_gate_smart"):
            return int(getattr(self._state, "turn_tool_calls", 0) or 0) >= 1
        return True

    async def aevaluate_turn_finish(
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
            elif cfg.get("finish_gate_smart"):
                reason = "finish_gate_smart_no_tool_calls"
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
            validation = await _maybe_await(avalidate_final_report_vs_user_intent(
                ctx=self._ctx,
                state=self._state,
                current_user_requests=current_user_intent,
                context_user_messages=user_intent_log,
                finish_handoff_arguments=finish_handoff_arguments,
            ))
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


def _evaluate_turn_finish_sync(self, *args: Any, **kwargs: Any):
    """Синхронный двойник судьи — для вызывающих вне цикла агента и тестов."""
    from gc_core.utils.portal import get_portal

    return get_portal().run(self.aevaluate_turn_finish(*args, **kwargs))


FinishGateGuard.evaluate_turn_finish = _evaluate_turn_finish_sync
