"""LLM-вызовы суммаризации через общий chat/completions client."""

from typing import Any, Dict, List, Optional

from config_modules.merge import deep_merge
from utils.llm_chat_client import request_chat_completion_text


def auxiliary_llm_completion_kwargs(ctx: Any) -> Dict[str, Any]:
    """
    Единый профиль chat/completions для вспомогательных LLM-вызовов:
    thread summary, finish gate, intent scope (см. ``chat_completions.llm`` в config).
    """
    chat_completions_llm = getattr(ctx, "chat_completions_llm", None)
    if chat_completions_llm is None:
        raise RuntimeError(
            "RunContext.chat_completions_llm обязателен; создавайте контекст через create_run_context."
        )
    completion_kwargs = getattr(chat_completions_llm, "completion_kwargs", None)
    if not isinstance(completion_kwargs, dict):
        raise RuntimeError(
            "RunContext.chat_completions_llm.completion_kwargs must be a dict"
        )
    return dict(completion_kwargs)


def request_summary_text(
    *,
    ctx: Any,
    state: Any,
    messages: List[Dict[str, str]],
    request_label: str,
    max_tokens: int | None = None,
    payload_overrides: Optional[Dict[str, Any]] = None,
) -> str:
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
    return request_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=messages,
        max_tokens=max_tokens if max_tokens is not None else default_max_tokens,
        request_label=request_label,
        payload_overrides=effective_overrides,
    )
