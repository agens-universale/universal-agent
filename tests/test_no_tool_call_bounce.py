from message_policy.no_tool_call_bounce import (
    NO_TOOL_CALL_BOUNCE_MESSAGE,
    extract_last_assistant_text,
    format_no_tool_call_bounce,
)


# --- extract_last_assistant_text ---


def test_extract_returns_empty_for_empty_messages():
    assert extract_last_assistant_text([]) == ""


def test_extract_handles_string_content():
    messages = [{"type": "message", "role": "assistant", "content": "hello"}]
    assert extract_last_assistant_text(messages) == "hello"


def test_extract_strips_outer_whitespace():
    messages = [{"type": "message", "role": "assistant", "content": "  hello  "}]
    assert extract_last_assistant_text(messages) == "hello"


def test_extract_returns_empty_for_whitespace_only():
    messages = [{"type": "message", "role": "assistant", "content": "   \n  "}]
    assert extract_last_assistant_text(messages) == ""


def test_extract_handles_list_content():
    messages = [{
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "first"}, {"text": "second"}],
    }]
    assert extract_last_assistant_text(messages) == "first\n\nsecond"


def test_extract_skips_empty_list_parts():
    messages = [{
        "type": "message",
        "role": "assistant",
        "content": [{"text": ""}, {"text": "real"}, {"text": "   "}],
    }]
    assert extract_last_assistant_text(messages) == "real"


def test_extract_returns_empty_for_non_assistant():
    messages = [{"type": "message", "role": "user", "content": "Hi"}]
    assert extract_last_assistant_text(messages) == ""


def test_extract_returns_empty_for_function_call_only():
    messages = [
        {"type": "function_call", "name": "x", "call_id": "c", "arguments": "{}"},
    ]
    assert extract_last_assistant_text(messages) == ""


def test_extract_finds_last_assistant_when_mixed():
    messages = [
        {"type": "message", "role": "user", "content": "Hi"},
        {"type": "message", "role": "assistant", "content": "First"},
        {"type": "function_call", "name": "x", "call_id": "c", "arguments": "{}"},
        {"type": "message", "role": "assistant", "content": "Second"},
    ]
    assert extract_last_assistant_text(messages) == "Second"


# --- format_no_tool_call_bounce ---


def test_format_falls_back_to_static_for_empty_text():
    assert format_no_tool_call_bounce("") == NO_TOOL_CALL_BOUNCE_MESSAGE


def test_format_falls_back_to_static_for_whitespace_text():
    assert format_no_tool_call_bounce("   \n  ") == NO_TOOL_CALL_BOUNCE_MESSAGE


def test_format_inlines_text_with_delimiters():
    msg = format_no_tool_call_bounce("Here is my plan.")
    assert "<<<\nHere is my plan.\n>>>" in msg


def test_format_contains_copy_verbatim_instruction():
    msg = format_no_tool_call_bounce("X")
    assert "VERBATIM" in msg
    assert "paraphrase" in msg.lower() or "summarize" in msg.lower()


def test_format_contains_two_retry_paths():
    msg = format_no_tool_call_bounce("X")
    assert "(1)" in msg and "(2)" in msg
    assert "action" in msg.lower() or "ACTION" in msg
    assert "ask_human" in msg


# --- decoupled from ask_human-cram wording ---

CRAM_MARKERS = (
    "FULL preceding content",
    "INSIDE the question parameter",
    "Do not strip",
    "tables, diagrams, architectures, options, recommendations",
    "paste the ENTIRE block above",
)


def test_static_bounce_has_no_cram_wording():
    for marker in CRAM_MARKERS:
        assert marker not in NO_TOOL_CALL_BOUNCE_MESSAGE, marker


def test_inlined_bounce_has_no_cram_wording():
    msg = format_no_tool_call_bounce("some assistant prose")
    for marker in CRAM_MARKERS:
        assert marker not in msg, marker


def test_bounce_keeps_robustness_signal():
    static = NO_TOOL_CALL_BOUNCE_MESSAGE
    inlined = format_no_tool_call_bounce("X")
    for msg in (static, inlined):
        assert "(1)" in msg and "(2)" in msg
        assert "ask_human" in msg
    assert "<<<\nX\n>>>" in inlined
    assert "VERBATIM" in inlined
