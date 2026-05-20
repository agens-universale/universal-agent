from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from config_modules.backend import parse_chat_completions_max_tokens

DEFAULT_LAST_DIALOG_STEPS_COUNT = 5
DEFAULT_SUMMARY_MAX_TOKENS = 8192
DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS = 1500
DEFAULT_MAX_MESSAGE_CHARS = 50000
DEFAULT_MAX_DIALOGUE_CHARS = 1000000
DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS = 20000
DEFAULT_ORIGINAL_USER_MESSAGES_MAX_CHARS = 20000
DEFAULT_INCREMENTAL_BATCH_MULTIPLIER = 1.4


class SummaryLlmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS
    completion_kwargs: dict[str, Any] = {}

    @field_validator("max_tokens", mode="before")
    @classmethod
    def normalize_max_tokens(cls, value: Any) -> int:
        return parse_chat_completions_max_tokens(value, default=DEFAULT_SUMMARY_MAX_TOKENS)

    @field_validator("completion_kwargs", mode="before")
    @classmethod
    def normalize_completion_kwargs(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        return {}


class SummaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_completions_summary_prompt_path: str | None = None
    chat_completions_summary_trigger_chars: int = 0
    incremental_batch_chars: int | None = None
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS
    max_dialogue_chars: int = DEFAULT_MAX_DIALOGUE_CHARS
    max_accumulated_summary_chars: int = DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS
    original_user_messages_max_chars: int = DEFAULT_ORIGINAL_USER_MESSAGES_MAX_CHARS
    last_dialog_steps_count: int = DEFAULT_LAST_DIALOG_STEPS_COUNT
    last_dialog_step_message_max_chars: int = DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS
    llm: SummaryLlmConfig = Field(default_factory=SummaryLlmConfig)


def summary_llm_from_summary_params(summary_params: dict[str, Any] | None) -> SummaryLlmConfig:
    """Собрать LLM-настройки суммаризации из ``summary_params`` (для тестов и child config без ``load_config``)."""
    params = summary_params if isinstance(summary_params, dict) else {}
    llm_raw = params.get("llm")
    if isinstance(llm_raw, SummaryLlmConfig):
        return llm_raw
    return SummaryLlmConfig.model_validate(llm_raw if isinstance(llm_raw, dict) else {})
