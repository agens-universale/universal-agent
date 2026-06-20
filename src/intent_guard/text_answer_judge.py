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


def judge_text_answer(ctx: Any, state: Any, assistant_text: str) -> bool:
    """True → принять текст как ответ хода; False → bounce (вкл. сбой судьи)."""
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


def should_return_text_answer(cfg: Any, ctx: Any, state: Any, assistant_text: str) -> bool:
    """Принять ли текст-ответ как ответ хода — по режиму ``cfg['text_answer_mode']``.

    ``always`` → да (без судьи); ``judge`` → решает судья; ``strict`` или ключа нет
    (автономка CLI/server/swarm) → нет. Ключ ставит только TUI (см. ``tui/app.py``)."""
    mode = (cfg or {}).get("text_answer_mode")
    if mode == "always":
        return True
    if mode == "judge":
        return judge_text_answer(ctx, state, assistant_text)
    return False
