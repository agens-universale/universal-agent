"""Аварийная LLM-суммаризация трейса при лимите (step/tool/token limit).

Spec: docs_md/actual/2026-08-01-limit-trace-summary-spec.md

Собирает envelope {"ok": false, "status": <reason>, "message": <short>,
"raw_trace_summary": <markdown|"">} через один aux LLM-вызов.
"""

import json
import logging
import os
from typing import Any

from gc_core.bundled_assets import bundled_root
from gc_core.summarize.llm import arequest_auxiliary_completion_text
from gc_core.usage.account import UsageAccount

_PROMPT_PATH = os.path.join(str(bundled_root()), "prompts", "limit_trace_summary.md")

_REASON_LABELS = {
    "step_limit": "step limit",
    "tool_call_limit": "tool-call limit",
    "token_limit": "token limit",
}

logger = logging.getLogger(__name__)

_prompt_cache: str | None = None


def _load_limit_summary_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is not None:
        return _prompt_cache
    with open(_PROMPT_PATH, "r", encoding="utf-8") as fd:
        text = fd.read().strip()
    if not text:
        raise ValueError(f"Limit trace summary prompt is empty: {_PROMPT_PATH}")
    _prompt_cache = text
    return text


def _tokens_spent(state: Any) -> Any:
    """Токены, потраченные с начала текущего run (как в run_agent token_limit)."""
    try:
        usage = getattr(state, "usage", None)
        if usage is None:
            return "?"
        baseline = UsageAccount.from_meta(
            getattr(state, "run_start_usage_meta", None)
        )
        return usage.delta_from(baseline).totals()["total"]
    except Exception:
        return "?"


def _build_message(state: Any, reason: str, config_dict: dict[str, Any]) -> str:
    label = _REASON_LABELS.get(reason, reason)
    if reason == "step_limit":
        return (
            f"Agent stopped: {label} "
            f"({state.steps}/{getattr(state, 'max_steps', '?')})."
        )
    if reason == "tool_call_limit":
        limit = config_dict.get("max_tool_calls")
        return (
            f"Agent stopped: {label} "
            f"({state.tool_calls}/{limit})."
        )
    if reason == "token_limit":
        limit = config_dict.get("session_token_limit")
        spent = _tokens_spent(state)
        return f"Agent stopped: {label} ({spent}/{limit})."
    return f"Agent stopped: {label}."


def _serialize_trace(state_messages: list[dict[str, Any]]) -> str:
    """Сериализует активный трейс (messages после system) в текст для промпта.

    Пропускает первое system-сообщение (system prompt агента). Не подмешивает
    shared/working memory — только то, что уже в state.messages.
    Обрабатывает весь трейс целиком, без лимита на размер.
    """
    msgs = list(state_messages or [])
    # Пропускаем ведущие system-сообщения.
    trace_msgs = [m for m in msgs if m.get("role") != "system"]
    if not trace_msgs and msgs:
        # Если все — system, берём всё (edge case).
        trace_msgs = msgs

    lines: list[str] = []
    for m in trace_msgs:
        mtype = m.get("type", "")

        # --- Tool call: {name, arguments, type:"function_call"} ---
        if mtype == "function_call":
            name = m.get("name", "?")
            args = m.get("arguments", "")
            text = str(args) if args is not None else ""
            lines.append(f"[tool_call:{name}] {text}")
            continue

        # --- Tool output: {output, type:"function_call_output"} ---
        if mtype == "function_call_output":
            output = m.get("output", "")
            text = str(output) if output is not None else ""
            lines.append(f"[tool_output] {text}")
            continue

        # --- Text message: {role, content, type:"message"} ---
        role = m.get("role", "?")
        content = m.get("content", "")
        name = m.get("name", "")
        # Компактное представление: role + content (и name если есть).
        tag = role
        if name:
            tag = f"{role}/{name}"
        text = str(content) if content is not None else ""
        lines.append(f"[{tag}] {text}")

    return "\n".join(lines)


def _should_enable(config_dict: dict[str, Any]) -> bool:
    raw = config_dict.get("limit_trace_summary")
    if raw is None:
        return True  # по умолчанию on
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


async def abuild_limit_envelope(state: Any, reason: str, ctx: Any) -> str:
    """Возвращает JSON-строку envelope для limit-path.

    Основная (и единственная) реализация: aux LLM-вызов уходит на тот же loop,
    что и цикл агента, — поток из пула на время суммаризации не занимается.

    Args:
        state: State с .messages, .steps, .tool_calls, .max_steps.
        reason: "step_limit" | "tool_call_limit" | "token_limit".
        ctx: RunContext (нужен config_dict и auxiliary_llm).

    Returns:
        JSON-строка {"ok": false, "status", "message", "raw_trace_summary"}.
    """
    config_dict = getattr(ctx, "config_dict", {}) or {}
    message = _build_message(state, reason, config_dict)

    raw_summary = ""
    if _should_enable(config_dict):
        max_tokens = getattr(getattr(ctx, "auxiliary_llm", None), "max_tokens", None)
        if not isinstance(max_tokens, int) or max_tokens < 1:
            max_tokens = 24576

        try:
            prompt_template = _load_limit_summary_prompt()
            trace_text = _serialize_trace(state.messages)
            user_content = prompt_template + "\n\n---\n## Agent trace\n\n" + trace_text
            messages = [
                {"role": "system", "content": "You are a trace summarizer."},
                {"role": "user", "content": user_content},
            ]
            raw_summary = await arequest_auxiliary_completion_text(
                ctx=ctx,
                state=state,
                messages=messages,
                max_tokens=max_tokens,
                request_label=f"limit_trace_summary:{reason}",
                consumer="summary",
                logger=getattr(ctx, "logger", None),
            )
            if raw_summary is None:
                raw_summary = ""
        except Exception:
            logger.warning(
                "limit_trace_summary LLM call failed for reason=%s; "
                "returning envelope with empty summary",
                reason,
                exc_info=True,
            )
            raw_summary = ""

    envelope = {
        "ok": False,
        "status": reason,
        "message": message,
        "raw_trace_summary": raw_summary,
    }
    return json.dumps(envelope, ensure_ascii=False)


