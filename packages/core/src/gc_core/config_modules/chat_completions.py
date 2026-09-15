from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

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
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    auxiliary_llm: ChatCompletionsLlmConfig = Field(
        default_factory=ChatCompletionsLlmConfig,
        validation_alias=AliasChoices("auxiliary_llm", "llm"),
    )


def _parse_auxiliary_llm_raw(raw: Any) -> ChatCompletionsLlmConfig | None:
    if isinstance(raw, ChatCompletionsLlmConfig):
        return raw
    if isinstance(raw, dict):
        return ChatCompletionsLlmConfig.model_validate(raw)
    return None


def _auxiliary_llm_from_params(cc_params: dict[str, Any]) -> ChatCompletionsLlmConfig | None:
    llm_raw = cc_params.get("auxiliary_llm")
    if llm_raw is None:
        llm_raw = cc_params.get("llm")  # legacy YAML-ключ внутри dump-секции
    return _parse_auxiliary_llm_raw(llm_raw)


def auxiliary_llm_from_config_dict(
    config_dict: dict[str, Any] | None,
) -> ChatCompletionsLlmConfig:
    """Собрать typed aux-профиль из плоского config_dict.

    Канон flat-ключа: ``auxiliary_llm_params`` (dict рядом с
    ``chat_completion_kwargs``). YAML-схема по-прежнему
    ``chat_completions.auxiliary_llm`` → flatten кладёт dump в
    ``auxiliary_llm_params``.

    Fallback (hand-rolled / старые бенч-оверлеи):
    ``chat_completions_params.auxiliary_llm`` / ``llm``, затем legacy
    ``chat_completions_llm``.
    """
    cfg = config_dict if isinstance(config_dict, dict) else {}

    parsed = _parse_auxiliary_llm_raw(cfg.get("auxiliary_llm_params"))
    if parsed is not None:
        return parsed

    cc_params = cfg.get("chat_completions_params")
    if isinstance(cc_params, dict):
        parsed = _auxiliary_llm_from_params(cc_params)
        if parsed is not None:
            return parsed

    parsed = _parse_auxiliary_llm_raw(cfg.get("chat_completions_llm"))
    if parsed is not None:
        return parsed

    return ChatCompletionsLlmConfig()


def subagent_role_max_tokens_from_ctx(ctx: Any) -> int:
    """``chat_completions.auxiliary_llm.subagent_role_max_tokens`` — лимит ответа для
    LLM-генерации роли special_agent (``ctx.auxiliary_llm``, с фоллбэком
    на ``ctx.config_dict`` для двойников без готового профиля)."""
    auxiliary_llm = getattr(ctx, "auxiliary_llm", None)
    if not isinstance(auxiliary_llm, ChatCompletionsLlmConfig):
        auxiliary_llm = auxiliary_llm_from_config_dict(
            getattr(ctx, "config_dict", None)
        )
    return auxiliary_llm.subagent_role_max_tokens
