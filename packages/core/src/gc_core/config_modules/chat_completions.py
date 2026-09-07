from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gc_core.config_modules.backend import parse_chat_completions_max_tokens

DEFAULT_AUXILIARY_LLM_MAX_TOKENS = 8192
# Judges (command_moderation, finish_gate / text_answer) often burn budget on
# mandatory CoT / <think> before JSON; keep a separate higher ceiling.
DEFAULT_JUDGE_MAX_TOKENS = 10000
# special_agent role generation (subagent:subagent_special_agent) — one-shot
# system-prompt draft, no CoT budget needed.
DEFAULT_SUBAGENT_ROLE_MAX_TOKENS = 4096


class ChatCompletionsLlmConfig(BaseModel):
    """LLM-профиль вспомогательных chat/completions-вызовов (summary, finish gate, intent scope, judges)."""

    model_config = ConfigDict(extra="forbid")

    max_tokens: int = DEFAULT_AUXILIARY_LLM_MAX_TOKENS
    judge_max_tokens: int = DEFAULT_JUDGE_MAX_TOKENS
    subagent_role_max_tokens: int = DEFAULT_SUBAGENT_ROLE_MAX_TOKENS
    completion_kwargs: dict[str, Any] = {}

    @field_validator("max_tokens", mode="before")
    @classmethod
    def normalize_max_tokens(cls, value: Any) -> int:
        return parse_chat_completions_max_tokens(
            value, default=DEFAULT_AUXILIARY_LLM_MAX_TOKENS
        )

    @field_validator("judge_max_tokens", mode="before")
    @classmethod
    def normalize_judge_max_tokens(cls, value: Any) -> int:
        return parse_chat_completions_max_tokens(
            value, default=DEFAULT_JUDGE_MAX_TOKENS
        )

    @field_validator("subagent_role_max_tokens", mode="before")
    @classmethod
    def normalize_subagent_role_max_tokens(cls, value: Any) -> int:
        return parse_chat_completions_max_tokens(
            value, default=DEFAULT_SUBAGENT_ROLE_MAX_TOKENS
        )

    @field_validator("completion_kwargs", mode="before")
    @classmethod
    def normalize_completion_kwargs(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        return {}


class ChatCompletionsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: ChatCompletionsLlmConfig = Field(default_factory=ChatCompletionsLlmConfig)


def chat_completions_llm_from_config_dict(
    config_dict: dict[str, Any] | None,
) -> ChatCompletionsLlmConfig:
    """Собрать LLM-профиль из ``chat_completions.llm`` в config."""
    cfg = config_dict if isinstance(config_dict, dict) else {}
    raw = cfg.get("chat_completions_llm")
    if isinstance(raw, ChatCompletionsLlmConfig):
        return raw

    cc_params = cfg.get("chat_completions_params")
    if isinstance(cc_params, dict):
        llm_raw = cc_params.get("llm")
        if isinstance(llm_raw, ChatCompletionsLlmConfig):
            return llm_raw
        if isinstance(llm_raw, dict):
            return ChatCompletionsLlmConfig.model_validate(llm_raw)

    return ChatCompletionsLlmConfig()


def subagent_role_max_tokens_from_ctx(ctx: Any) -> int:
    """``chat_completions.llm.subagent_role_max_tokens`` — лимит ответа для
    LLM-генерации роли special_agent (``ctx.chat_completions_llm``, с фоллбэком
    на ``ctx.config_dict`` для двойников без готового профиля)."""
    chat_completions_llm = getattr(ctx, "chat_completions_llm", None)
    if not isinstance(chat_completions_llm, ChatCompletionsLlmConfig):
        chat_completions_llm = chat_completions_llm_from_config_dict(
            getattr(ctx, "config_dict", None)
        )
    return chat_completions_llm.subagent_role_max_tokens
