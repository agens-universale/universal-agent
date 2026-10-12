from pydantic import BaseModel, ConfigDict, Field

from gc_core.config_modules.field_meta import gc_meta

DEFAULT_LAST_DIALOG_STEPS_COUNT = 5
DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS = 1500
DEFAULT_MAX_MESSAGE_CHARS = 50000
DEFAULT_MAX_DIALOGUE_CHARS = 1000000
DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS = 20000
DEFAULT_INCREMENTAL_BATCH_MULTIPLIER = 1.2
DEFAULT_INTENT_LOG_PREVIEW_CHARS = 10000
DEFAULT_TURN_END_TRIGGER_RATIO = 0.6
# Потолок хвоста диалога в режиме модели: × от символьного основания.
DEFAULT_MAX_DIALOGUE_CHARS_MULTIPLIER = 5
# Грубый курс символов истории к токенам: оценка хвоста токенного порога и
# батч суммаризации, когда задан только токенный порог.
SUMMARY_CHARS_PER_TOKEN = 2


def derived_turn_end_trigger(mid: int) -> int:
    """Turn-end, когда ключ не задан: 60% от mid, не меньше 1 при mid > 0."""
    if mid <= 0:
        return 0
    return max(1, int(mid * DEFAULT_TURN_END_TRIGGER_RATIO))


def summary_char_base(chars: int, tokens: int) -> int:
    """Символьное основание батча и лимита диалога.

    Символьный порог, если он > 0; иначе токенный × SUMMARY_CHARS_PER_TOKEN.
    """
    if chars > 0:
        return chars
    if tokens > 0:
        return tokens * SUMMARY_CHARS_PER_TOKEN
    return 0


def derived_incremental_batch_chars(char_base: int) -> int:
    """Батч, когда ключ не задан: основание × 1.2."""
    if char_base <= 0:
        return 0
    return int(char_base * DEFAULT_INCREMENTAL_BATCH_MULTIPLIER)


def derived_max_dialogue_chars(char_base: int) -> int:
    """Лимит диалога режима модели: основание × 5."""
    if char_base <= 0:
        return 0
    return int(char_base * DEFAULT_MAX_DIALOGUE_CHARS_MULTIPLIER)


class SummaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    chat_completions_summary_prompt_path: str | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    chat_completions_memory_patch_replace_prompt_path: str | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    chat_completions_summary_trigger_chars: int = 0
    chat_completions_summary_turn_end_trigger_chars: int | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    chat_completions_summary_trigger_tokens: int = 0
    chat_completions_summary_turn_end_trigger_tokens: int | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    incremental_batch_chars: int | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS
    max_dialogue_chars: int = DEFAULT_MAX_DIALOGUE_CHARS
    max_accumulated_summary_chars: int = DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS
    last_dialog_steps_count: int = DEFAULT_LAST_DIALOG_STEPS_COUNT
    last_dialog_step_message_max_chars: int = (
        DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS
    )
    intent_log_preview_chars: int = Field(
        default=DEFAULT_INTENT_LOG_PREVIEW_CHARS,
        json_schema_extra=gc_meta(expert=True),
    )
