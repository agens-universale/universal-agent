from __future__ import annotations

import logging
from typing import Any, List

from gc_core.agent_store import AgentStore, agent_store_of, aread_required
from gc_core.config_modules.intent_scope import IntentScopeConfig
from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.resources import (
    PromptResourceRequest,
    aload_prompt_resource,
)
from gc_core.intent_log.serialize import (
    deserialize_list,
    records_to_jsonl_lines,
)
from gc_core.intent_scope.agent_logging import (
    log_intent_scope_summary_done,
    log_intent_scope_summary_start,
)
from gc_core.message_format.dialog_history import truncate_text
from gc_core.summarize.llm import arequest_summary_text

_logger = logging.getLogger(__name__)


async def aload_intent_prompt(store: AgentStore, name: str) -> str:
    """Промпт intent scope (``intent_tool`` или ``intent_final``)."""
    # Путь задаёт конфиг (`intent_scope_*_prompt_path`) — файл человека, его
    # кодировку никто не проверял. Отказ обязан назвать файл.
    label = store.label("prompt", name)
    system = (
        await aread_required(
            store,
            "prompt",
            name,
            missing=f"Intent scope prompt file not found: {label}",
        )
    ).strip()
    if not system:
        raise ValueError(f"Intent scope prompt file is empty: {label}")
    return system


def _scope_cfg(ctx: Any) -> IntentScopeConfig:
    cfg = getattr(ctx, "intent_scope", None)
    if isinstance(cfg, IntentScopeConfig):
        return cfg
    return IntentScopeConfig()


def _format_prior_intent(current_user_intent: List[Any]) -> str:
    lines = records_to_jsonl_lines(deserialize_list(current_user_intent or []))
    if not lines:
        return "(empty)"
    return "\n".join(lines)


def _build_user_prompt(
    *,
    tool_name: str,
    tool_arguments: str,
    tool_output: str,
    prior_intent: List[Any],
    max_tool_output_chars: int,
) -> str:
    args_text = truncate_text(str(tool_arguments or ""), max_tool_output_chars)
    output_text = truncate_text(str(tool_output or ""), max_tool_output_chars)
    return (
        f"tool_name: {tool_name}\n\n"
        f"tool_arguments:\n{args_text}\n\n"
        f"tool_output:\n{output_text}\n\n"
        f"prior_intent:\n{_format_prior_intent(prior_intent)}\n"
    )


async def asummarize_tool_intent_line(
    *,
    ctx: Any,
    state: Any,
    tool_name: str,
    tool_arguments: str,
    tool_output_raw: str,
    logger: Any | None = None,
) -> str:
    """
    LLM extraction of one intent line from raw tool output. Fail-open: returns '' on error.

    Основная (и единственная) реализация. Суммаризация — сетевой вызов на
    десятки секунд; держать ради него поток из пула незачем, поэтому цикл
    агента зовёт эту корутину напрямую. Синхронное имя ниже — шим поверх неё.
    """
    log = logger if logger is not None else _logger
    scope_cfg = _scope_cfg(ctx)
    output_stripped = str(tool_output_raw or "").strip()
    if not output_stripped:
        return ""

    log_intent_scope_summary_start(
        log, tool_name=tool_name, output_chars=len(output_stripped)
    )

    prior = list(getattr(state, "current_user_intent", []) or [])
    request_messages = [
        {
            "role": "system",
            "content": await aload_prompt_resource(
                ctx,
                PromptResourceRequest("intent_tool"),
                local=lambda: aload_intent_prompt(
                    agent_store_of(ctx), "intent_tool"
                ),
            ),
        },
        {
            "role": "user",
            "content": _build_user_prompt(
                tool_name=tool_name,
                tool_arguments=tool_arguments,
                tool_output=output_stripped,
                prior_intent=prior,
                max_tool_output_chars=scope_cfg.max_tool_output_chars,
            ),
        },
    ]
    request_label = f"intent_scope:{tool_name}"
    try:
        line = await arequest_summary_text(
            ctx=ctx,
            state=state,
            messages=request_messages,
            request_label=request_label,
            consumer="intent_scope",
            logger=log,
        )
    except ExecutionFailure:
        raise
    except Exception as exc:
        log.warning(
            "intent_scope summary_failed tool=%s error=%s",
            tool_name,
            exc,
            exc_info=True,
        )
        return ""

    if not line:
        log_intent_scope_summary_done(
            log, tool_name=tool_name, summary_chars=0
        )
        return ""
    if scope_cfg.max_summary_chars > 0:
        line = truncate_text(line, scope_cfg.max_summary_chars)
    log_intent_scope_summary_done(
        log, tool_name=tool_name, summary_chars=len(line)
    )
    return line


def build_fallback_aggregated_content(
    *,
    ctx: Any,
    tool_output_raw: str,
) -> str:
    """Truncated tool output for aggregated record when LLM summary fails.

    Tool output as-is (no strip-tags projection): contamination is in model prose /
    aux LLM replies, not in opaque tool results.
    """
    scope_cfg = _scope_cfg(ctx)
    body = str(tool_output_raw or "").strip()
    if not body:
        return ""
    max_chars = scope_cfg.max_summary_chars
    if max_chars > 0:
        body = truncate_text(body, max_chars)
    return body


async def asummarize_final_answer_line(
    *,
    ctx: Any,
    state: Any,
    assistant_text: str,
    logger: Any | None = None,
) -> str:
    """LLM compression of a long final prose answer into 1–3 sentences. Fail-open: '' on error.

    Основная (и единственная) реализация — по тем же причинам, что и у
    ``asummarize_tool_intent_line``. Синхронное имя ниже — шим поверх неё.
    """
    log = logger if logger is not None else _logger
    scope_cfg = _scope_cfg(ctx)
    text = str(assistant_text or "").strip()
    if not text:
        return ""

    log_intent_scope_summary_start(
        log, tool_name="final_answer", output_chars=len(text)
    )

    prior = list(getattr(state, "current_user_intent", []) or [])
    body = truncate_text(text, scope_cfg.max_tool_output_chars)
    request_label = "intent_scope:final_answer"
    try:
        request_messages = [
            {
                "role": "system",
                "content": await aload_prompt_resource(
                    ctx,
                    PromptResourceRequest("intent_final"),
                    local=lambda: aload_intent_prompt(
                        agent_store_of(ctx), "intent_final"
                    ),
                ),
            },
            {
                "role": "user",
                "content": (
                    f"final_answer:\n{body}\n\nprior_intent:\n{_format_prior_intent(prior)}\n"
                ),
            },
        ]
        line = await arequest_summary_text(
            ctx=ctx,
            state=state,
            messages=request_messages,
            request_label=request_label,
            consumer="intent_scope",
            logger=log,
        )
    except ExecutionFailure:
        raise
    except Exception as exc:
        log.warning(
            "intent_scope summary_failed tool=final_answer error=%s",
            exc,
            exc_info=True,
        )
        return ""

    if not line:
        log_intent_scope_summary_done(
            log, tool_name="final_answer", summary_chars=0
        )
        return ""
    if scope_cfg.max_summary_chars > 0:
        line = truncate_text(line, scope_cfg.max_summary_chars)
    log_intent_scope_summary_done(
        log, tool_name="final_answer", summary_chars=len(line)
    )
    return line
