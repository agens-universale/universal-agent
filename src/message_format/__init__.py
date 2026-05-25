"""Каноничное форматирование сообщений треда для LLM (суммаризация, finish gate, промпты).

Зависит от ``message_policy`` (technical user, dialog summary). Feature-модули
(``summarize``, ``intent_guard``) импортируют отсюда, а не наоборот.
"""

from message_format.dialog_history import (
    build_history_text,
    format_last_dialog_steps,
    group_into_turns,
    message_text_limited,
    split_turns_into_char_batches,
    trim_history_to_max_dialogue_chars,
    trim_original_user_messages_to_max_chars,
    truncate_text,
)
from message_format.serialization import (
    calc_history_chars_for_trigger,
    calc_messages_chars_total,
    history_has_dialog_summary_message,
    message_raw_payload_chars_for_trigger,
    message_text_for_summary,
    strip_original_user_messages_block,
)

__all__ = [
    "build_history_text",
    "calc_history_chars_for_trigger",
    "calc_messages_chars_total",
    "format_last_dialog_steps",
    "group_into_turns",
    "history_has_dialog_summary_message",
    "message_raw_payload_chars_for_trigger",
    "message_text_for_summary",
    "message_text_limited",
    "split_turns_into_char_batches",
    "strip_original_user_messages_block",
    "trim_history_to_max_dialogue_chars",
    "trim_original_user_messages_to_max_chars",
    "truncate_text",
]
