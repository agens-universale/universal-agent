from pydantic import BaseModel, ConfigDict, Field

DEFAULT_LAST_DIALOG_STEPS_COUNT = 5
DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS = 1500
DEFAULT_MAX_MESSAGE_CHARS = 50000
DEFAULT_MAX_DIALOGUE_CHARS = 1000000
DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS = 20000
DEFAULT_INCREMENTAL_BATCH_MULTIPLIER = 1.4
DEFAULT_INTENT_LOG_PREVIEW_CHARS = 10000


class SummaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_completions_summary_prompt_path: str | None = None
    chat_completions_memory_patch_append_prompt_path: str | None = None
    chat_completions_memory_patch_replace_prompt_path: str | None = None
    chat_completions_summary_trigger_chars: int = 0
    incremental_batch_chars: int | None = None
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS
    max_dialogue_chars: int = DEFAULT_MAX_DIALOGUE_CHARS
    max_accumulated_summary_chars: int = DEFAULT_MAX_ACCUMULATED_SUMMARY_CHARS
    last_dialog_steps_count: int = DEFAULT_LAST_DIALOG_STEPS_COUNT
    last_dialog_step_message_max_chars: int = DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS
    intent_log_preview_chars: int = DEFAULT_INTENT_LOG_PREVIEW_CHARS
