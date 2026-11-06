"""trace_mapper: raw thread_store-payload → TraceEvent. Базовые kinds."""

from gc_tui.services.trace_mapper import map_message


def test_user_message():
    ev = map_message(
        {"role": "user", "content": "объясни код"},
        no=1,
        ts="2026-05-20T10:00:00+00:00",
    )
    assert ev.kind == "user"
    assert ev.summary == "объясни код"
    assert ev.no == 1


def test_assistant_text_message():
    ev = map_message(
        {"role": "assistant", "content": "вот ответ"},
        no=2,
        ts="2026-05-20T10:00:01+00:00",
    )
    assert ev.kind == "agent"
    assert ev.summary == "вот ответ"


def test_think_message():
    ev = map_message(
        {"type": "reasoning", "summary": "размышляю"},
        no=3,
        ts="2026-05-20T10:00:02+00:00",
    )
    assert ev is None


def test_unknown_returns_none():
    assert (
        map_message({"foo": "bar"}, no=1, ts="2026-05-20T10:00:00+00:00")
        is None
    )


def test_assistant_string_content_renders():
    """Plain string content (responses backend) → kind=agent."""
    msg = {"role": "assistant", "content": "Hello", "id": "m1"}
    ev = map_message(msg, no=1, ts="2026-05-22T12:00:00+00:00")
    assert ev is not None
    assert ev.kind == "agent"
    assert ev.summary == "Hello"


def test_assistant_list_content_renders():
    """chat_completions list-shape content → kind=agent with concatenated text."""
    msg = {
        "role": "assistant",
        "content": [
            {"type": "output_text", "text": "First part. ", "annotations": []},
            {"type": "output_text", "text": "Second part.", "annotations": []},
        ],
        "id": "m1",
    }
    ev = map_message(msg, no=1, ts="2026-05-22T12:00:00+00:00")
    assert ev is not None
    assert ev.kind == "agent"
    assert ev.summary == "First part. Second part."


def test_assistant_think_block_stripped():
    """<think>...</think> block removed from rendered content."""
    msg = {
        "role": "assistant",
        "content": "<think>internal reasoning here</think>\n\nActual response to user.",
        "id": "m1",
    }
    ev = map_message(msg, no=1, ts="2026-05-22T12:00:00+00:00")
    assert ev is not None
    assert ev.kind == "agent"
    assert "internal reasoning" not in ev.summary
    assert "Actual response to user." in ev.summary


def test_assistant_only_think_returns_none():
    """If content is ONLY <think>...</think> with no post-think text → None."""
    msg = {
        "role": "assistant",
        "content": "<think>thinking but no answer</think>",
        "id": "m1",
    }
    ev = map_message(msg, no=1, ts="2026-05-22T12:00:00+00:00")
    assert ev is None


def test_assistant_list_content_with_think():
    """list-shape content with <think> in text → strip think, return remainder."""
    msg = {
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": "<think>reasoning</think>\n\nFinal answer.",
                "annotations": [],
            },
        ],
        "id": "m1",
    }
    ev = map_message(msg, no=1, ts="2026-05-22T12:00:00+00:00")
    assert ev is not None
    assert ev.kind == "agent"
    assert ev.summary == "Final answer."
    assert "reasoning" not in ev.summary


def test_assistant_empty_content_returns_none():
    msg = {"role": "assistant", "content": "", "id": "m1"}
    ev = map_message(msg, no=1, ts="2026-05-22T12:00:00+00:00")
    assert ev is None


def test_assistant_none_content_returns_none():
    msg = {"role": "assistant", "content": None, "id": "m1"}
    ev = map_message(msg, no=1, ts="2026-05-22T12:00:00+00:00")
    assert ev is None


def test_skill_message_carries_skill_id():
    ev = map_message(
        {
            "role": "user",
            "content": "WIRE\nпридумай",
            "display_content": "придумай",
            "skill_loaded": "supercoder:brainstorming",
        },
        no=1,
        ts="",
    )
    assert ev.kind == "user"
    assert ev.summary == "придумай"
    assert ev.skill == "supercoder:brainstorming"


def test_skill_only_delivery_is_reply_with_empty_text():
    ev = map_message(
        {
            "role": "user",
            "content": "WIRE",
            "display_content": "",
            "skill_loaded": "qa-x",
        },
        no=1,
        ts="",
    )
    assert ev.kind == "user"
    assert ev.summary == ""
    assert ev.skill == "qa-x"


def test_empty_display_without_skill_is_not_rendered():
    msg = {"role": "user", "content": "WIRE", "display_content": ""}
    assert map_message(msg, no=1, ts="") is None


def test_plain_user_message_has_no_skill():
    ev = map_message({"role": "user", "content": "привет"}, no=1, ts="")
    assert ev.skill is None
