from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from gc_core.config_modules.backend import parse_chat_completions_max_tokens

DEFAULT_AUXILIARY_LLM_MAX_TOKENS = 24576
# Judges (command_moderation, finish_gate / text_answer): запас под мысль модели.
# В TUI нельзя выключить thinking у aux; люди ставят «думающую» модель без правок
# YAML — мысль съедает бюджет, после strip_think JSON пустой/обрезан. Schema
# default без vendor-ключей (OpenAI 400), поэтому потолок выше. max_tokens —
# потолок, не квота: модель может ответить короче.
DEFAULT_JUDGE_MAX_TOKENS = 24576
# special_agent role draft (subagent:subagent_special_agent): one-shot system
# prompt; идёт через aux-воронку (kwargs + strip_think). Запас под мысль модели
# при пустых completion_kwargs (TUI не гасит thinking).
DEFAULT_SUBAGENT_ROLE_MAX_TOKENS = 24576


class ChatCompletionsLlmConfig(BaseModel):
    """LLM-профиль вспомогательных chat/completions-вызовов (summary, finish gate, intent scope, judges).

    ``completion_kwargs`` — overlay поверх agent ``chat_completion_kwargs``.
    Schema default ``{}`` (без vendor-ключей); thinking/anti-thinking задаются
    в YAML/бенч-профиле, не в коде.
    """

    model_config = ConfigDict(extra="forbid")

    max_tokens: int = DEFAULT_AUXILIARY_LLM_MAX_TOKENS
    judge_max_tokens: int = DEFAULT_JUDGE_MAX_TOKENS
    subagent_role_max_tokens: int = DEFAULT_SUBAGENT_ROLE_MAX_TOKENS
    completion_kwargs: dict[str, Any] = {}

    @field_validator("max_tokens", mode="before")
    @classmethod
    def normalize_max_tokens(cls, value: Any) -> int:
        return parse_chat_completions_max_tokens(value, default=DEFAULT_AUXILIARY_LLM_MAX_TOKENS)

    @field_validator("judge_max_tokens", mode="before")
    @classmethod
    def normalize_judge_max_tokens(cls, value: Any) -> int:
        return parse_chat_completions_max_tokens(value, default=DEFAULT_JUDGE_MAX_TOKENS)

    @field_validator("subagent_role_max_tokens", mode="before")
    @classmethod
    def normalize_subagent_role_max_tokens(cls, value: Any) -> int:
        return parse_chat_completions_max_tokens(value, default=DEFAULT_SUBAGENT_ROLE_MAX_TOKENS)

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


def _auxiliary_llm_dump_if_non_default(raw: Any) -> dict[str, Any] | None:
    """Dump профиля, если он отличается от schema defaults; иначе None.

    Flatten всегда кладёт дефолтный ``auxiliary_llm_params`` рядом с таким же
    dump в ``chat_completions_params.auxiliary_llm``. Сравниваем с дефолтом,
    чтобы выбрать первый «живой» слой: flat → nested → legacy → schema.
    """
    parsed = _parse_auxiliary_llm_raw(raw)
    if parsed is None:
        return None
    dump = parsed.model_dump()
    if dump == ChatCompletionsLlmConfig().model_dump():
        return None
    return dump


def auxiliary_llm_from_config_dict(
    config_dict: dict[str, Any] | None,
) -> ChatCompletionsLlmConfig:
    """Собрать typed aux-профиль из плоского config_dict.

    Канон flat-ключа: ``auxiliary_llm_params`` (dict рядом с
    ``chat_completion_kwargs``). YAML-схема по-прежнему
    ``chat_completions.auxiliary_llm`` → flatten кладёт dump в
    ``auxiliary_llm_params``.

    Приоритет (первый слой, отличающийся от schema defaults):
    1. ``auxiliary_llm_params`` (канон harbor / deep_merge)
    2. ``chat_completions_params.auxiliary_llm`` / ``llm`` (старые оверлеи)
    3. legacy ``chat_completions_llm``
    4. schema defaults (``completion_kwargs: {}``)
    """
    cfg = config_dict if isinstance(config_dict, dict) else {}

    flat = _auxiliary_llm_dump_if_non_default(cfg.get("auxiliary_llm_params"))
    if flat is not None:
        return ChatCompletionsLlmConfig.model_validate(flat)

    cc_params = cfg.get("chat_completions_params")
    if isinstance(cc_params, dict):
        nested = _auxiliary_llm_dump_if_non_default(cc_params.get("auxiliary_llm"))
        if nested is None:
            nested = _auxiliary_llm_dump_if_non_default(cc_params.get("llm"))
        if nested is not None:
            return ChatCompletionsLlmConfig.model_validate(nested)

    legacy = _auxiliary_llm_dump_if_non_default(cfg.get("chat_completions_llm"))
    if legacy is not None:
        return ChatCompletionsLlmConfig.model_validate(legacy)

    return ChatCompletionsLlmConfig()


def subagent_role_max_tokens_from_ctx(ctx: Any) -> int:
    """``chat_completions.auxiliary_llm.subagent_role_max_tokens`` — лимит ответа для
    LLM-генерации роли special_agent (``ctx.auxiliary_llm``, с фоллбэком
    на ``ctx.config_dict`` для двойников без готового профиля)."""
    auxiliary_llm = getattr(ctx, "auxiliary_llm", None)
    if not isinstance(auxiliary_llm, ChatCompletionsLlmConfig):
        auxiliary_llm = auxiliary_llm_from_config_dict(getattr(ctx, "config_dict", None))
    return auxiliary_llm.subagent_role_max_tokens
