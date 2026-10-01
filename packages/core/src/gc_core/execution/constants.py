"""Policy constants shared by local and durable bindings."""

_MAX_CONTEXT_OVERFLOW_RETRIES = 3

_SESSION_COMPACT_RESUME_USER_TEXT = (
    "session_compact finished. The dialog summary user message in the trace is the new "
    "baseline (updated session memory, last_dialog_steps, user intent). Continue from "
    "that summary. The pre-compaction step-by-step trace is no longer in context."
)

_TRUNCATED_ARGUMENTS_HINT = (
    "⚠ Output was truncated: output token limit reached "
    '(finish_reason == "length"). Shorten the response '
    "or split it into multiple calls with smaller payloads."
)
