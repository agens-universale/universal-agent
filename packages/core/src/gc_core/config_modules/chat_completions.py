from __future__ import annotations

from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from gc_core.config_modules.backend import parse_chat_completions_max_tokens
from gc_core.config_modules.field_meta import gc_meta

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

    model_config = ConfigDict(extra="forbid", frozen=True)

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
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, frozen=True
    )

    auxiliary_llm: ChatCompletionsLlmConfig = Field(
        default_factory=ChatCompletionsLlmConfig,
        validation_alias=AliasChoices("auxiliary_llm", "llm"),
        json_schema_extra=gc_meta(expert=True),
    )


def auxiliary_llm_from_config_dict(
    settings: Any,
) -> ChatCompletionsLlmConfig:
    """Typed aux-профиль: ``chat_completions.auxiliary_llm`` настроек.

    ``settings`` — всё, что принимает ``settings_of``. Плоские
    ``auxiliary_llm_params`` и ``chat_completions_params.auxiliary_llm`` ведут в
    одно поле модели (лист перекрывает секцию — прежний приоритет слоёв).
    """
    # Ленивый импорт: settings → root → этот модуль.
    from gc_core.settings import settings_of

    return settings_of(settings).chat_completions.auxiliary_llm


def subagent_role_max_tokens_from_ctx(ctx: Any) -> int:
    """``chat_completions.auxiliary_llm.subagent_role_max_tokens`` — лимит ответа для
    LLM-генерации роли special_agent (``ctx.auxiliary_llm``, с фоллбэком
    на настройки контекста для двойников без готового профиля)."""
    auxiliary_llm = getattr(ctx, "auxiliary_llm", None)
    if not isinstance(auxiliary_llm, ChatCompletionsLlmConfig):
        auxiliary_llm = auxiliary_llm_from_config_dict(ctx)
    return auxiliary_llm.subagent_role_max_tokens
