"""TUI adaptive bounce: оценка текст-ответа finish-gate-судьёй.

Когда модель в TUI отвечает текстом без тула, этот судья решает: текст адекватно
закрывает интент юзера (accept → текст и есть ответ хода) или модель отлынивает
от задачи (bounce → подтолкнуть к тулам). Переиспользует существующего finish-gate-
судью, подавая текст как «отчёт»; контекст (intent + log + trace_digest) судья
собирает сам. Сбой судьи → bounce (строгий fallback)."""
from __future__ import annotations

from typing import Any

from intent_log import deserialize_list, records_to_jsonl_lines
from intent_guard.final_report_consistency import validate_final_report_vs_user_intent
from intent_guard.guard import FinishGateGuard


def judge_text_answer(ctx: Any, state: Any, assistant_text: str) -> bool:
    """True → принять текст как ответ хода; False → bounce.

    Исключение валидатора → False (bounce). Замечание: при неразборчивом ответе
    LLM нижележащий валидатор fail-open'ит в ``accept`` (как finish-gate) → текст
    будет принят; это редкий путь и согласован с поведением finish-gate."""
    try:
        current = records_to_jsonl_lines(
            deserialize_list(getattr(state, "current_user_intent", []) or [])
        )
        context_log = records_to_jsonl_lines(
            deserialize_list(getattr(state, "user_intent_log", []) or [])
        )
        result = validate_final_report_vs_user_intent(
            ctx=ctx,
            state=state,
            current_user_requests=current,
            context_user_messages=context_log,
            finish_handoff_arguments=assistant_text,
        )
        return getattr(result, "action", "") == "accept"
    except Exception:
        return False


def should_return_text_answer(ctx: Any, state: Any, assistant_text: str) -> bool:
    """Принять ли текст-ответ как ответ хода — по режиму ``text_answer_mode`` из cfg.

    ``always`` → да (без судьи); ``judge`` → решает судья, но только если включён
    сам finish-gate (``chat_completions`` + ``strict_turn_finish_validation``) — иначе
    судью не на чём построить (в т.ч. responses-режим), ведём себя как ``strict``;
    ``strict`` или ключа нет (автономка CLI/server/swarm) → нет. Ключ ставит только
    TUI (см. ``tui/app.py``)."""
    cfg = getattr(ctx, "config_dict", {}) or {}
    mode = cfg.get("text_answer_mode") if isinstance(cfg, dict) else None
    if mode == "always":
        return True
    if mode == "judge":
        # Судья переиспользует finish-gate → активен только там, где включён сам
        # finish-gate. Нет гейта (responses / strict-off) → строго (bounce).
        if not FinishGateGuard(ctx=ctx, state=state).enabled:
            return False
        return judge_text_answer(ctx, state, assistant_text)
    return False
