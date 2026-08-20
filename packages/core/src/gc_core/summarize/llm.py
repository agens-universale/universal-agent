"""LLM-вызовы суммаризации через общий chat/completions client."""

from typing import Any, Dict, List, Optional

from gc_core.config_modules.merge import deep_merge
from gc_core.message_projection import project_text_for_model
from gc_core.utils.llm_chat_client import arequest_chat_completion_text, log_llm_prompt_and_result


async def _maybe_await(value: Any):
    """Шов подмены терпит обе формы заглушки — см. processing_responses._maybe_await."""
    import inspect

    if inspect.isawaitable(value):
        return await value
    return value


def _require_chat_completions_llm(ctx: Any) -> Any:
    chat_completions_llm = getattr(ctx, "chat_completions_llm", None)
    if chat_completions_llm is None:
        raise RuntimeError(
            "RunContext.chat_completions_llm обязателен; создавайте контекст через create_run_context."
        )
    return chat_completions_llm


def auxiliary_llm_completion_kwargs(ctx: Any) -> Dict[str, Any]:
    """
    Единый профиль chat/completions для вспомогательных LLM-вызовов:
    thread summary, finish gate, intent scope (см. ``chat_completions.llm`` в config).
    """
    chat_completions_llm = _require_chat_completions_llm(ctx)
    completion_kwargs = getattr(chat_completions_llm, "completion_kwargs", None)
    if not isinstance(completion_kwargs, dict):
        raise RuntimeError(
            "RunContext.chat_completions_llm.completion_kwargs must be a dict"
        )
    return dict(completion_kwargs)


def auxiliary_llm_judge_max_tokens(ctx: Any) -> int:
    """
    Лимит ответа для LLM-судей (command_moderation, finish gate / text_answer).

    Читает ``chat_completions.llm.judge_max_tokens`` (отдельно от summary ``max_tokens``).
    """
    from gc_core.config_modules.chat_completions import DEFAULT_JUDGE_MAX_TOKENS

    chat_completions_llm = _require_chat_completions_llm(ctx)
    value = getattr(chat_completions_llm, "judge_max_tokens", None)
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
    payload_overrides: Optional[Dict[str, Any]] = None,
    logger: Any | None = None,
    log_prompt: bool = True,
) -> str:
    """Aux LLM: log dirty response, return ``for_model``-projected text.

    Main agent ``request_chat_completion_text`` stays dirty (canonical persist).
    """
    raw = await _maybe_await(arequest_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=messages,
        max_tokens=max_tokens,
        request_label=request_label,
        payload_overrides=payload_overrides,
    ))
    if log_prompt:
        active_logger = logger if logger is not None else getattr(ctx, "logger", None)
        log_llm_prompt_and_result(
            logger=active_logger,
            label=request_label,
            messages=messages,
            output_text=raw,
        )
    return project_text_for_model(raw)


async def arequest_summary_text(
    *,
    ctx: Any,
    state: Any,
    messages: List[Dict[str, str]],
    request_label: str,
    max_tokens: int | None = None,
    payload_overrides: Optional[Dict[str, Any]] = None,
    logger: Any | None = None,
) -> str:
    """Summary/intent-scope aux call: dirty logged, projected text returned."""
    completion_kwargs = auxiliary_llm_completion_kwargs(ctx)
    effective_overrides = completion_kwargs
    if isinstance(payload_overrides, dict) and payload_overrides:
        effective_overrides = deep_merge(completion_kwargs, payload_overrides)
    chat_completions_llm = getattr(ctx, "chat_completions_llm", None)
    default_max_tokens = getattr(chat_completions_llm, "max_tokens", None)
    if not isinstance(default_max_tokens, int) or default_max_tokens < 1:
        raise RuntimeError(
            "RunContext.chat_completions_llm.max_tokens must be a positive int"
        )
    return await arequest_auxiliary_completion_text(
        ctx=ctx,
        state=state,
        messages=messages,
        max_tokens=max_tokens if max_tokens is not None else default_max_tokens,
        request_label=request_label,
        payload_overrides=effective_overrides,
        logger=logger,
    )


# --- синхронные двойники ---------------------------------------------------
#
# Нужны вызывающим вне цикла агента (ручной /compact в TUI, тесты). Внутри
# асинхронного цикла звать их НЕЛЬЗЯ: портал поднимет
# SyncApiInsideEventLoopError — и это правильно, потому что fail-open вокруг
# судей проглотил бы такую ошибку и молча выключил проверку.


def request_auxiliary_completion_text(**kwargs: Any) -> str:
    from gc_core.utils.portal import get_portal

    return get_portal().run(arequest_auxiliary_completion_text(**kwargs))


def request_summary_text(**kwargs: Any) -> str:
    from gc_core.utils.portal import get_portal

    return get_portal().run(arequest_summary_text(**kwargs))
