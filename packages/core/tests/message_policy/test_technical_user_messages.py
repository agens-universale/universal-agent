"""Служебное сообщение узнаётся по началу текста (спека §3.4): маркер в
середине — слова человека, вставившего ошибку или цитату служебного текста."""

import pytest

from gc_core.agent_edits.feedback import build_undo_feedback_message
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_CURRENT_CONTEXT,
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    TECHNICAL_TYPE_ERROR_MESSAGE,
    TECHNICAL_TYPE_TECHNICAL,
    build_invalid_tool_call_feedback_message,
    extract_real_user_message_text,
    is_technical_user_message_text,
    wrap_skill_resolution_technical_instruction,
    wrap_technical_user_content,
)

PASTED_ERROR = (
    "Почини вызов, вот что пишет агент:\n"
    "Tool argument validation failed for fs_read_file.\n"
    "Expected schema: {'type': 'object'}\n"
    "Received arguments: {}"
)


def test_pasted_validation_error_with_a_request_is_the_users_words():
    assert is_technical_user_message_text(PASTED_ERROR) is False
    message = {"role": "user", "content": PASTED_ERROR}
    assert extract_real_user_message_text(message) == PASTED_ERROR


def test_a_quoted_technical_tag_in_the_middle_is_the_users_words():
    text = 'Что значит <technical type="error_message"> в логе?'
    assert is_technical_user_message_text(text) is False


@pytest.mark.parametrize(
    "content",
    [
        *(
            wrap_technical_user_content("x", technical_type=kind)
            for kind in (
                TECHNICAL_TYPE_TECHNICAL,
                TECHNICAL_TYPE_ERROR_MESSAGE,
                TECHNICAL_TYPE_DIALOG_SUMMARY,
                TECHNICAL_TYPE_CURRENT_CONTEXT,
            )
        ),
        wrap_technical_user_content(""),
        wrap_skill_resolution_technical_instruction("deploy"),
        build_invalid_tool_call_feedback_message(
            {"name": "fs_write_file", "arguments": "{"}
        )["content"],
        build_undo_feedback_message(["a.py"])["content"],
        # формы из старых тредов
        "<current_context>client has non-empty thread notes; call "
        '`thread_notes` with action="read" to load them.</current_context>',
        "There is no tool call in Agent response. Please send a tool call",
        "❌ **Tool Argument Validation Failed**\n\n**Error:** x",
        "<dialog_summary>\n...\n</dialog_summary>",
        '[user][technical] <technical type="technical">x</technical>',
        '  \n<technical type="technical">x</technical>',
    ],
)
def test_every_kind_of_technical_message_is_still_recognised(content):
    assert is_technical_user_message_text(content) is True
