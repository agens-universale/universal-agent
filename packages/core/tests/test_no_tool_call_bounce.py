from gc_core.message_policy.no_tool_call_bounce import (
    CONTENT_FILTER_BOUNCE_MESSAGE,
    LENGTH_BOUNCE_MESSAGE,
    NO_TOOL_CALL_BOUNCE_MESSAGE,
    PROVIDER_BLOCKED_BOUNCE_MESSAGE,
    extract_last_assistant_text,
    format_no_tool_call_bounce,
    is_content_filter_finish,
)

# --- extract_last_assistant_text ---


def test_extract_returns_empty_for_empty_messages():
    assert extract_last_assistant_text([]) == ""


def test_extract_handles_string_content():
    messages = [{"type": "message", "role": "assistant", "content": "hello"}]
    assert extract_last_assistant_text(messages) == "hello"


def test_extract_strips_outer_whitespace():
    messages = [
        {"type": "message", "role": "assistant", "content": "  hello  "}
    ]
    assert extract_last_assistant_text(messages) == "hello"


def test_extract_returns_empty_for_whitespace_only():
    messages = [{"type": "message", "role": "assistant", "content": "   \n  "}]
    assert extract_last_assistant_text(messages) == ""


def test_extract_handles_list_content():
    messages = [
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "first"},
                {"text": "second"},
            ],
        }
    ]
    assert extract_last_assistant_text(messages) == "first\n\nsecond"


def test_extract_skips_empty_list_parts():
    messages = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"text": ""}, {"text": "real"}, {"text": "   "}],
        }
    ]
    assert extract_last_assistant_text(messages) == "real"


def test_extract_returns_empty_for_non_assistant():
    messages = [{"type": "message", "role": "user", "content": "Hi"}]
    assert extract_last_assistant_text(messages) == ""


def test_extract_returns_empty_for_function_call_only():
    messages = [
        {
            "type": "function_call",
            "name": "x",
            "call_id": "c",
            "arguments": "{}",
        },
    ]
    assert extract_last_assistant_text(messages) == ""


def test_extract_finds_last_assistant_when_mixed():
    messages = [
        {"type": "message", "role": "user", "content": "Hi"},
        {"type": "message", "role": "assistant", "content": "First"},
        {
            "type": "function_call",
            "name": "x",
            "call_id": "c",
            "arguments": "{}",
        },
        {"type": "message", "role": "assistant", "content": "Second"},
    ]
    assert extract_last_assistant_text(messages) == "Second"


# --- format_no_tool_call_bounce ---


def test_format_is_static_for_empty_text():
    assert format_no_tool_call_bounce("") == NO_TOOL_CALL_BOUNCE_MESSAGE


def test_format_is_static_for_whitespace_text():
    assert format_no_tool_call_bounce("   \n  ") == NO_TOOL_CALL_BOUNCE_MESSAGE


def test_format_does_not_inline_assistant_prose():
    msg = format_no_tool_call_bounce("Here is my plan.")
    assert msg == NO_TOOL_CALL_BOUNCE_MESSAGE
    assert "Here is my plan." not in msg
    assert "<<<" not in msg
    assert "VERBATIM" not in msg


def test_format_uses_content_filter_nudge():
    msg = format_no_tool_call_bounce(
        "blocked prose", finish_reason="content_filter"
    )
    assert msg == CONTENT_FILTER_BOUNCE_MESSAGE
    assert msg == PROVIDER_BLOCKED_BOUNCE_MESSAGE
    assert "blocked prose" not in msg
    assert "content_filter" in msg
    assert "Rephrase" in msg


def test_format_blocked_reasons_share_nudge():
    for fr in (
        "content_filter",
        "refusal",
        "safety",
        "Content_Filter",
        "REFUSAL",
    ):
        assert (
            format_no_tool_call_bounce(finish_reason=fr)
            == PROVIDER_BLOCKED_BOUNCE_MESSAGE
        )


def test_format_uses_length_nudge():
    msg = format_no_tool_call_bounce("partial prose", finish_reason="length")
    assert msg == LENGTH_BOUNCE_MESSAGE
    assert "truncated" in msg.lower()
    assert "length" in msg
    assert "partial prose" not in msg


def test_format_content_filter_is_case_insensitive():
    assert (
        format_no_tool_call_bounce(finish_reason="Content_Filter")
        == CONTENT_FILTER_BOUNCE_MESSAGE
    )
    assert is_content_filter_finish("CONTENT_FILTER")
    assert is_content_filter_finish("refusal")
    assert is_content_filter_finish("safety")
    assert not is_content_filter_finish("stop")
    assert not is_content_filter_finish("length")
    assert not is_content_filter_finish(None)


def test_bounce_asks_for_a_tool_call():
    assert "no tool call" in NO_TOOL_CALL_BOUNCE_MESSAGE.lower()
    assert "tool call" in NO_TOOL_CALL_BOUNCE_MESSAGE.lower()
    assert "closer to solving the task" in NO_TOOL_CALL_BOUNCE_MESSAGE.lower()


def test_bounce_stays_tool_agnostic():
    # Никаких хардкод-имён тулов — согласуется с tool-agnostic промптом master.
    assert "turn_finish" not in NO_TOOL_CALL_BOUNCE_MESSAGE
    assert "turn_ask_human" not in NO_TOOL_CALL_BOUNCE_MESSAGE
    assert "submit_solution" not in NO_TOOL_CALL_BOUNCE_MESSAGE


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
