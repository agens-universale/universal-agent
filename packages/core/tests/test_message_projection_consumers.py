"""TDD: call-sites for_model — format_messages, serialization, extract."""

from gc_core.message_format.dialog_history import build_history_text
from gc_core.message_format.serialization import (
    calc_history_chars_for_trigger,
    message_raw_payload_chars_for_trigger,
    message_text_for_summary,
)
from gc_core.message_policy.no_tool_call_bounce import (
    NO_TOOL_CALL_BOUNCE_MESSAGE,
    extract_last_assistant_text,
    format_no_tool_call_bounce,
)
from gc_core.utils.format_messages import (
    format_message_chat_completions_to_send_backend,
)


def test_format_canonical_assistant_keeps_think_prose_as_in_messages():
    msg = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "<think>r</think>Ответ"}],
    }
    out = format_message_chat_completions_to_send_backend(msg)
    assert out["role"] == "assistant"
    assert out["content"] == "<think>r</think>Ответ"


def test_format_canonical_only_think_kept():
    msg = {
        "type": "message",
        "role": "assistant",
        "content": "<think>only reasoning</think>",
    }
    out = format_message_chat_completions_to_send_backend(msg)
    assert out["content"] == "<think>only reasoning</think>"


def test_format_legacy_assistant_keeps_think():
    msg = {
        "role": "assistant",
        "content": "<think>x</think>Y",
        "tool_calls": [],
    }
    out = format_message_chat_completions_to_send_backend(msg)
    assert out["content"] == "<think>x</think>Y"


def test_format_user_with_think_literal_untouched():
    msg = {
        "type": "message",
        "role": "user",
        "content": "цитата <think>не стрипать</think>",
    }
    out = format_message_chat_completions_to_send_backend(msg)
    assert out["content"] == "цитата <think>не стрипать</think>"


def test_format_prose_only_unchanged():
    msg = {"type": "message", "role": "assistant", "content": "просто ответ"}
    out = format_message_chat_completions_to_send_backend(msg)
    assert out["content"] == "просто ответ"


def test_format_sends_reasoning_content_when_present_in_canon():
    msg = {
        "type": "message",
        "role": "assistant",
        "content": "<think>r</think>Ответ",
        "reasoning_content": "secret",
    }
    out = format_message_chat_completions_to_send_backend(msg)
    assert out["reasoning_content"] == "secret"
    assert out["content"] == "<think>r</think>Ответ"


def test_format_legacy_sends_reasoning_content_when_present_in_canon():
    msg = {
        "role": "assistant",
        "content": "<think>r</think>Ответ",
        "reasoning_content": "secret",
        "tool_calls": [],
    }
    out = format_message_chat_completions_to_send_backend(msg)
    assert out["reasoning_content"] == "secret"
    assert out["content"] == "<think>r</think>Ответ"


def test_format_omits_reasoning_content_when_absent_from_canon():
    msg = {
        "type": "message",
        "role": "assistant",
        "content": "Ответ",
    }
    out = format_message_chat_completions_to_send_backend(msg)
    assert "reasoning_content" not in out


def test_trigger_volume_excludes_think_span():
    raw = "<think>длинное рассуждение</think>короткий"
    msg = {"type": "message", "role": "assistant", "content": raw}
    assert message_raw_payload_chars_for_trigger(msg) == len("короткий")
    assert calc_history_chars_for_trigger([msg]) == len("короткий")


def test_trigger_volume_think_only_counts_raw_payload():
    raw = "<think>search the leaderboard</think>"
    msg = {"type": "message", "role": "assistant", "content": raw}
    assert message_raw_payload_chars_for_trigger(msg) == len(raw)
    assert calc_history_chars_for_trigger([msg]) == len(raw)


def test_trigger_volume_think_only_list_content_counts_raw():
    text = "<think>search the leaderboard</think>"
    msg = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    assert message_raw_payload_chars_for_trigger(msg) == len(text)


def test_trigger_volume_user_keeps_literal_think():
    text = "user said <think>literal</think>"
    msg = {"type": "message", "role": "user", "content": text}
    assert message_raw_payload_chars_for_trigger(msg) == len(text)


def test_message_text_for_summary_assistant_without_think():
    msg = {
        "type": "message",
        "role": "assistant",
        "content": "<think>secret</think>visible",
    }
    text = message_text_for_summary(msg)
    assert text == "[assistant] visible"
    assert "<think" not in text


def test_message_text_for_summary_includes_reasoning_tail():
    msg = {
        "type": "message",
        "role": "assistant",
        "content": "",
        "reasoning_content": "s041 wrong; s075 correct; s078 WA 38",
    }
    text = message_text_for_summary(msg)
    assert "[assistant]" in text
    assert "[reasoning]" in text
    assert "s075 correct" in text
    assert "s078 WA 38" in text


def test_message_text_for_summary_reasoning_tail_is_capped():
    from gc_core.message_format.serialization import _REASONING_TAIL_MAX_CHARS

    long_rc = ("HEAD" * 800) + ("TAIL_MARKER_" + "z" * 40)
    assert len(long_rc) > _REASONING_TAIL_MAX_CHARS
    msg = {
        "type": "message",
        "role": "assistant",
        "content": "ok",
        "reasoning_content": long_rc,
    }
    text = message_text_for_summary(msg)
    reasoning_body = text.split("[reasoning]\n", 1)[1]
    assert reasoning_body == long_rc[-_REASONING_TAIL_MAX_CHARS:]
    assert "TAIL_MARKER_" in reasoning_body


def test_message_text_for_summary_uses_openrouter_reasoning_alias():
    msg = {
        "type": "message",
        "role": "assistant",
        "content": "",
        "reasoning": "alias-only verdict",
    }
    text = message_text_for_summary(msg)
    assert "[reasoning]" in text
    assert "alias-only verdict" in text


def test_trigger_volume_counts_reasoning_tail():
    msg = {
        "type": "message",
        "role": "assistant",
        "content": "visible",
        "reasoning_content": "hidden facts",
    }
    assert message_raw_payload_chars_for_trigger(msg) == len("visible") + len(
        "hidden facts"
    )


def test_build_history_text_smoke_no_think_in_assistant():
    messages = [
        {"type": "message", "role": "user", "content": "hi"},
        {
            "type": "message",
            "role": "assistant",
            "content": "<think>r</think>done",
        },
    ]
    history = build_history_text(messages)
    assert "<think" not in history
    assert "done" in history


def test_extract_strips_think_from_last_assistant():
    messages = [
        {
            "type": "message",
            "role": "assistant",
            "content": "<think>r</think>Hello user",
        },
    ]
    assert extract_last_assistant_text(messages) == "Hello user"


def test_extract_only_think_returns_empty():
    messages = [
        {
            "type": "message",
            "role": "assistant",
            "content": "<think>only</think>",
        },
    ]
    assert extract_last_assistant_text(messages) == ""


def test_bounce_from_extract_has_no_think():
    messages = [
        {
            "type": "message",
            "role": "assistant",
            "content": "<think>plan</think>Need a tool call.",
        },
    ]
    text = extract_last_assistant_text(messages)
    bounce = format_no_tool_call_bounce(text)
    assert "<think" not in bounce
    assert bounce == NO_TOOL_CALL_BOUNCE_MESSAGE
