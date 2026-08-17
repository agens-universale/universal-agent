"""TUI adaptive bounce: оценка текст-ответа finish-gate-судьёй.

Когда модель в TUI отвечает текстом без тула, этот судья решает: текст адекватно
закрывает интент юзера (accept → текст и есть ответ хода) или модель отлынивает
от задачи (bounce → подтолкнуть к тулам). Переиспользует существующего finish-gate-
судью, подавая текст как «отчёт»; контекст (intent + log + trace_digest) судья
собирает сам. Сбой судьи → bounce (строгий fallback)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gc_core.intent_log import deserialize_list, records_to_jsonl_lines
from gc_core.intent_guard.final_report_consistency import validate_final_report_vs_user_intent
from gc_core.intent_guard.guard import judge_available
from gc_core.intent_guard.feedback import build_text_answer_reject_feedback
from gc_core.intent_guard.validation_result import ValidationResult


@dataclass(frozen=True)
class TextAnswerDecision:
    accept: bool
    reject_feedback: str | None = None


def judge_text_answer(ctx: Any, state: Any, assistant_text: str) -> ValidationResult | None:
    """Результат валидатора для текст-ответа; None → ошибка (→ generic bounce)."""
    try:
        current = records_to_jsonl_lines(
            deserialize_list(getattr(state, "current_user_intent", []) or [])
        )
        context_log = records_to_jsonl_lines(
            deserialize_list(getattr(state, "user_intent_log", []) or [])
        )
        return validate_final_report_vs_user_intent(
            ctx=ctx,
            state=state,
            current_user_requests=current,
            context_user_messages=context_log,
            finish_handoff_arguments=assistant_text,
        )
    except Exception:
        return None


def should_return_text_answer(ctx: Any, state: Any, assistant_text: str) -> TextAnswerDecision:
    """Принять ли текст-ответ как ответ хода — по режиму ``text_answer_mode``.

    Пустой текст → reject (любой режим). ``allow`` → accept без судьи. ``judge`` →
    судья (только если доступна машинерия судейства, т.е. ``backend_api_mode ==
    "chat_completions"``; иначе deny-путь) — от тоггла /gate (finish-gate) НЕ зависит:
    accept → accept; reject → reject c фидбэком судьи; ошибка судьи → reject generic.
    ``deny`` / нет ключа → reject generic."""
    if not (assistant_text or "").strip():
        return TextAnswerDecision(False)
    cfg = getattr(ctx, "config_dict", {}) or {}
    mode = cfg.get("text_answer_mode") if isinstance(cfg, dict) else None
    if mode == "allow":
        return TextAnswerDecision(True)
    if mode == "judge":
        if not judge_available(cfg):
            return TextAnswerDecision(False)
        result = judge_text_answer(ctx, state, assistant_text)
        if result is None:
            return TextAnswerDecision(False)
        if getattr(result, "action", "") == "accept":
            return TextAnswerDecision(True)
        return TextAnswerDecision(False, build_text_answer_reject_feedback(result))
    return TextAnswerDecision(False)
