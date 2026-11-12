"""Аварийная LLM-суммаризация трейса при лимите (step/tool/token limit).

Spec: docs_md/actual/2026-08-01-limit-trace-summary-spec.md

Собирает envelope {"ok": false, "status": <reason>, "message": <short>,
"raw_trace_summary": <markdown|"">} через один aux LLM-вызов.
"""

import json
import logging
from typing import Any

from gc_core.agent_store import (
    AgentStore,
    FileAgentStore,
    Prompt,
    agent_store_of,
    decode_text,
)
from gc_core.execution.contracts import ExecutionFailure, PayloadTooLarge
from gc_core.execution.resources import (
    PromptResourceRequest,
    aload_prompt_resource,
)
from gc_core.settings import AppConfig, settings_of
from gc_core.usage.account import UsageAccount

_REASON_LABELS = {
    "step_limit": "step limit",
    "tool_call_limit": "tool-call limit",
    "token_limit": "token limit",
}

logger = logging.getLogger(__name__)

_prompt_cache: str | None = None


async def aload_limit_summary_prompt(store: AgentStore) -> str:
    global _prompt_cache
    # Кэш — только у пакетного файла: своё хранилище отвечает само.
    cacheable = type(store) is FileAgentStore
    if cacheable and _prompt_cache is not None:
        return _prompt_cache
    data = await store.read("prompt", Prompt.LIMIT_SUMMARY)
    if data is None:
        raise FileNotFoundError(store.label("prompt", "limit_summary"))
    text = decode_text(data).strip()
    if not text:
        raise ValueError(
            "Limit trace summary prompt is empty: "
            f"{store.label('prompt', 'limit_summary')}"
        )
    if cacheable:
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


def _build_message(state: Any, reason: str, settings: AppConfig) -> str:
    label = _REASON_LABELS.get(reason, reason)
    if reason == "step_limit":
        return f"Agent stopped: {label} ({state.steps}/{getattr(state, 'max_steps', '?')})."
    if reason == "tool_call_limit":
        limit = settings.agent.max_tool_calls
        return f"Agent stopped: {label} ({state.tool_calls}/{limit})."
    if reason == "token_limit":
        limit = settings.agent.session_token_limit
        spent = _tokens_spent(state)
        return f"Agent stopped: {label} ({spent}/{limit})."
    return f"Agent stopped: {label}."


def _trace_line(m: dict[str, Any]) -> str:
    mtype = m.get("type", "")
    # --- Tool call: {name, arguments, type:"function_call"} ---
    if mtype == "function_call":
        args = m.get("arguments", "")
        text = str(args) if args is not None else ""
        return f"[tool_call:{m.get('name', '?')}] {text}"
    # --- Tool output: {output, type:"function_call_output"} ---
    if mtype == "function_call_output":
        output = m.get("output", "")
        text = str(output) if output is not None else ""
        return f"[tool_output] {text}"
    # --- Text message: {role, content, type:"message"} ---
    role = m.get("role", "?")
    content = m.get("content", "")
    name = m.get("name", "")
    # Компактное представление: role + content (и name если есть).
    tag = f"{role}/{name}" if name else role
    text = str(content) if content is not None else ""
    return f"[{tag}] {text}"


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
        lines.append(_trace_line(m))

    return "\n".join(lines)


async def abuild_limit_envelope(state: Any, reason: str, ctx: Any) -> str:
    """Возвращает JSON-строку envelope для limit-path.

    Основная (и единственная) реализация: aux LLM-вызов уходит на тот же loop,
    что и цикл агента, — поток из пула на время суммаризации не занимается.

    Args:
        state: State с .messages, .steps, .tool_calls, .max_steps.
        reason: "step_limit" | "tool_call_limit" | "token_limit".
        ctx: RunContext (нужны настройки и auxiliary_llm).

    Returns:
        JSON-строка {"ok": false, "status", "message", "raw_trace_summary"}.
    """
    settings = settings_of(ctx)
    message = _build_message(state, reason, settings)

    raw_summary = ""
    # agent.limit_trace_summary — bool модели, по умолчанию on.
    if settings.agent.limit_trace_summary:
        max_tokens = getattr(
            getattr(ctx, "auxiliary_llm", None), "max_tokens", None
        )
        if not isinstance(max_tokens, int) or max_tokens < 1:
            max_tokens = 24576

        try:
            prompt_template = await aload_prompt_resource(
                ctx,
                PromptResourceRequest("limit_summary"),
                local=lambda: aload_limit_summary_prompt(agent_store_of(ctx)),
            )
            trace_text = _serialize_trace(state.messages)
            user_content = (
                prompt_template + "\n\n---\n## Agent trace\n\n" + trace_text
            )
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
        except PayloadTooLarge:
            # Трасса больше записи durable-истории: как отказ провайдера
            # локально (контекст такого размера модель не примет) — ход
            # кончается со своим статусом и пустой сводкой, а не ошибкой хода.
            logger.warning(
                "limit_trace_summary skipped for reason=%s: trace too large",
                reason,
            )
            raw_summary = ""
        except ExecutionFailure:
            raise
        except Exception:
            logger.warning(
                "limit_trace_summary LLM call failed for reason=%s; returning envelope with empty summary",
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


async def arequest_auxiliary_completion_text(**kwargs):
    from gc_core.summarize.llm import (
        arequest_auxiliary_completion_text as request,
    )

    return await request(**kwargs)
