"""LLM-вызовы суммаризации через общий chat/completions client."""

from typing import Any, Dict, List, Optional

from gc_core.config_modules.merge import deep_merge
from gc_core.config_modules.model_registry import effective_completion_kwargs
from gc_core.config_modules.strip_tags_projection import (
    projection_tags_from_config,
)
from gc_core.message_projection import project_text_for_model
from gc_core.model_router import AliasCircuitOpenError, completion_section
from gc_core.utils.llm_chat_client import (
    arequest_chat_completion_text,
    log_llm_prompt_and_result,
)


def _require_auxiliary_llm(ctx: Any) -> Any:
    auxiliary_llm = getattr(ctx, "auxiliary_llm", None)
    if auxiliary_llm is None:
        raise RuntimeError(
            "RunContext.auxiliary_llm обязателен; создавайте контекст через create_run_context."
        )
    return auxiliary_llm


def auxiliary_llm_completion_kwargs(
    ctx: Any, consumer: str | None = None
) -> Dict[str, Any]:
    """Kwargs служебного вызова: ``auxiliary`` алиаса, куда резолвится consumer.

    Сигнатура ``(ctx)`` сохранена: без consumer это ``summary`` (так зовёт CLI).
    Секции ``auxiliary`` нет — ``chat_completions.auxiliary_llm.completion_kwargs``
    из снимка конфига. Объект ``auxiliary_llm.completion_kwargs`` сам по себе
    не читается. Субагент получает ``main``.
    """
    consumer = consumer or "summary"
    cfg = getattr(ctx, "config_dict", None)
    resolved = None
    router = getattr(ctx, "model_router", None)
    if router is not None:
        try:
            resolved = router.resolve(consumer, check_breaker=False)
        except AliasCircuitOpenError as exc:
            resolved = exc.resolved
        except ValueError:
            resolved = None
    return effective_completion_kwargs(
        resolved,
        cfg if isinstance(cfg, dict) else {},
        section=completion_section(consumer),
    )


def auxiliary_llm_judge_max_tokens(ctx: Any) -> int:
    """
    Лимит ответа для LLM-судей (command_moderation, finish gate / text_answer).

    Читает ``auxiliary_llm_params.judge_max_tokens`` / YAML
    ``chat_completions.auxiliary_llm.judge_max_tokens`` (отдельно от summary ``max_tokens``).
    """
    from gc_core.config_modules.chat_completions import (
        DEFAULT_JUDGE_MAX_TOKENS,
    )

    auxiliary_llm = _require_auxiliary_llm(ctx)
    value = getattr(auxiliary_llm, "judge_max_tokens", None)
    if isinstance(value, int) and value >= 1:
        return value
    return DEFAULT_JUDGE_MAX_TOKENS


async def arequest_auxiliary_completion_text(
    *,
    ctx: Any,
    state: Any,
    messages: List[Dict[str, str]],
    max_tokens: int,
    request_label: str,
    consumer: str,
    payload_overrides: Optional[Dict[str, Any]] = None,
    logger: Any | None = None,
    log_prompt: bool = True,
) -> str:
    """Aux LLM: log dirty response, return ``for_model``-projected text.

    Main agent ``request_chat_completion_text`` stays dirty (canonical persist).
    ``consumer`` — canonical id из ``ModelRouter.CONSUMERS`` (спека §5.1); маппинг на
    существующие ``request_label`` не меняется (usage-учёт как был).

    ``<think>`` на aux всегда вырезается. Persist ``reasoning_content`` —
    только канон ``state.messages``, не судья / finish-gate / summary.
    Extra-теги из ``strip_tags_projection.tags`` по-прежнему применяются.
    """
    raw = await arequest_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=messages,
        max_tokens=max_tokens,
        request_label=request_label,
        consumer=consumer,
        payload_overrides=payload_overrides,
    )
    if log_prompt:
        active_logger = (
            logger if logger is not None else getattr(ctx, "logger", None)
        )
        log_llm_prompt_and_result(
            logger=active_logger,
            label=request_label,
            messages=messages,
            output_text=raw,
        )
    tags = projection_tags_from_config(getattr(ctx, "config_dict", None))
    return project_text_for_model(raw, tags=tags, strip_think=True)


async def arequest_summary_text(
    *,
    ctx: Any,
    state: Any,
    messages: List[Dict[str, str]],
    request_label: str,
    consumer: str,
    max_tokens: int | None = None,
    payload_overrides: Optional[Dict[str, Any]] = None,
    logger: Any | None = None,
) -> str:
    """Summary/intent-scope aux call: dirty logged, projected text returned."""
    completion_kwargs = auxiliary_llm_completion_kwargs(ctx, consumer)
    effective_overrides = completion_kwargs
    if isinstance(payload_overrides, dict) and payload_overrides:
        effective_overrides = deep_merge(completion_kwargs, payload_overrides)
    auxiliary_llm = getattr(ctx, "auxiliary_llm", None)
    default_max_tokens = getattr(auxiliary_llm, "max_tokens", None)
    if not isinstance(default_max_tokens, int) or default_max_tokens < 1:
        raise RuntimeError(
            "RunContext.auxiliary_llm.max_tokens must be a positive int"
        )
    return await arequest_auxiliary_completion_text(
        ctx=ctx,
        state=state,
        messages=messages,
        max_tokens=max_tokens
        if max_tokens is not None
        else default_max_tokens,
        request_label=request_label,
        consumer=consumer,
        payload_overrides=effective_overrides,
        logger=logger,
    )


#
# Нужны вызывающим вне цикла агента (ручной /compact в TUI, тесты). Внутри
# асинхронного цикла звать их НЕЛЬЗЯ — и не потому, что «что-то откажет»:
# синхронный вызов заморозил бы loop на всё время aux-LLM, а fail-open вокруг
# судей проглотил бы такую ошибку и молча выключил проверку.
