from processing_responses import (
    _extract_last_assistant_text,
    _format_no_tool_call_bounce,
    NO_TOOL_CALL_BOUNCE_MESSAGE,
)


# --- _extract_last_assistant_text ---


def test_extract_returns_empty_for_empty_messages():
    assert _extract_last_assistant_text([]) == ""


def test_extract_handles_string_content():
    messages = [{"type": "message", "role": "assistant", "content": "hello"}]
    assert _extract_last_assistant_text(messages) == "hello"


def test_extract_strips_outer_whitespace():
    messages = [{"type": "message", "role": "assistant", "content": "  hello  "}]
    assert _extract_last_assistant_text(messages) == "hello"


def test_extract_returns_empty_for_whitespace_only():
    messages = [{"type": "message", "role": "assistant", "content": "   \n  "}]
    assert _extract_last_assistant_text(messages) == ""


def test_extract_handles_list_content():
    messages = [{
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "first"}, {"text": "second"}],
    }]
    assert _extract_last_assistant_text(messages) == "first\n\nsecond"


def test_extract_skips_empty_list_parts():
    messages = [{
        "type": "message",
        "role": "assistant",
        "content": [{"text": ""}, {"text": "real"}, {"text": "   "}],
    }]
    assert _extract_last_assistant_text(messages) == "real"


def test_extract_returns_empty_for_non_assistant():
    messages = [{"type": "message", "role": "user", "content": "Hi"}]
    assert _extract_last_assistant_text(messages) == ""


def test_extract_returns_empty_for_function_call_only():
    messages = [
        {"type": "function_call", "name": "x", "call_id": "c", "arguments": "{}"},
    ]
    assert _extract_last_assistant_text(messages) == ""


def test_extract_finds_last_assistant_when_mixed():
    messages = [
        {"type": "message", "role": "user", "content": "Hi"},
        {"type": "message", "role": "assistant", "content": "First"},
        {"type": "function_call", "name": "x", "call_id": "c", "arguments": "{}"},
        {"type": "message", "role": "assistant", "content": "Second"},
    ]
    assert _extract_last_assistant_text(messages) == "Second"


# --- _format_no_tool_call_bounce ---


def test_format_falls_back_to_static_for_empty_text():
    assert _format_no_tool_call_bounce("") == NO_TOOL_CALL_BOUNCE_MESSAGE


def test_format_falls_back_to_static_for_whitespace_text():
    assert _format_no_tool_call_bounce("   \n  ") == NO_TOOL_CALL_BOUNCE_MESSAGE


def test_format_inlines_text_with_delimiters():
    msg = _format_no_tool_call_bounce("Here is my plan.")
    assert "<<<\nHere is my plan.\n>>>" in msg


def test_format_contains_copy_verbatim_instruction():
    msg = _format_no_tool_call_bounce("X")
    assert "VERBATIM" in msg
    assert "paraphrase" in msg.lower() or "summarize" in msg.lower()


def test_format_contains_two_retry_paths():
    msg = _format_no_tool_call_bounce("X")
    assert "(1)" in msg and "(2)" in msg
    assert "action" in msg.lower() or "ACTION" in msg
    assert "ask_human" in msg
