"""Completed turn outcomes in the intent log (caller layer, not State)."""

from __future__ import annotations

import json
import logging
from typing import Any

from gc_core.intent_scope.agent_logging import (
    log_intent_scope_delta_appended,
    log_intent_scope_skipped,
)
from gc_core.intent_scope.append import append_final_answer_to_current
from gc_core.settings import settings_of
from gc_core.utils.text_preview import head_tail_preview

_logger = logging.getLogger(__name__)
_LABEL = "final_answer"


def _append(state: Any, content: str, log: Any) -> None:
    if append_final_answer_to_current(state, content):
        log_intent_scope_delta_appended(
            log, tool_name=_LABEL, delta_chars=len(content)
        )


def record_turn_finish_outcome(*, arguments: str, outcome: str, state: Any) -> None:
    """Разрешённый итог — в журнал до flush/compaction, без служебного запроса."""
    payload = json.loads(arguments)
    kind = str(payload.get("solution_type") or "").strip()
    report = str(payload.get("task_report") or "").strip()
    prefix = kind
    if outcome in ("fail_open_error", "fail_open_limit"):
        prefix += f" [{outcome}]"
    prefix = f"{prefix}: " if prefix else ""
    text = prefix + head_tail_preview(report, 1000 - len(prefix))
    append_final_answer_to_current(state, text)


async def amaybe_record_final_answer_intent(
    *, assistant_text: str, state: Any, ctx: Any
) -> None:
    """
    Record an ACCEPTED final prose answer into ``current_user_intent`` (fail-open).

    chat_completions only. Short prose (<= threshold) is stored raw as-is;
    long prose is summarized; on summary failure — truncated raw (if fallback) or skip.
    Does not depend on ``intent_scope.tools`` (final answer is not a tool).

    Основная (и единственная) реализация. Ветка со сжатием длинной прозы ходит
    в LLM на десятки секунд, поэтому цикл агента ждёт эту корутину напрямую —
    поток из пула ради ожидания сети не занимается. Синхронное имя ниже — шим.
    """
    log = getattr(ctx, "logger", None) or _logger
    text = str(assistant_text or "").strip()
    if not text:
        log_intent_scope_skipped(log, tool_name=_LABEL, reason="empty_text")
        return

    if (
        str(settings_of(ctx).backend.api_mode).strip().lower()
        != "chat_completions"
    ):
        log_intent_scope_skipped(
            log, tool_name=_LABEL, reason="backend_not_chat_completions"
        )
        return

    scope_cfg = getattr(ctx, "intent_scope", None)
    threshold = int(getattr(scope_cfg, "max_final_answer_raw_chars", 0) or 0)

    if threshold > 0 and len(text) <= threshold:
        _append(state, text, log)
        return

    summary = await asummarize_final_answer_line(
        ctx=ctx, state=state, assistant_text=text, logger=log
    )
    if summary:
        _append(state, summary, log)
        return

    if scope_cfg is not None and getattr(
        scope_cfg, "fallback_raw_on_llm_error", False
    ):
        max_raw = int(getattr(scope_cfg, "max_raw_record_chars", 0) or 0)
        body = text[:max_raw] if max_raw > 0 else text
        _append(state, body, log)
        return

    log_intent_scope_skipped(log, tool_name=_LABEL, reason="empty_summary")


async def asummarize_final_answer_line(**kwargs):
    from gc_core.intent_scope.summarizer import (
        asummarize_final_answer_line as summarize,
    )

    return await summarize(**kwargs)
