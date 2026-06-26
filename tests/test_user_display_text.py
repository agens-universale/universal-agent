from message_policy.user_message_policy import (
    user_display_text, extract_real_user_message_text,
)


def test_display_text_prefers_display_content():
    msg = {"role": "user", "content": "<skill…>\nдавай обсудим", "display_content": "давай обсудим"}
    assert user_display_text(msg) == "давай обсудим"


def test_display_text_empty_string_is_kept():
    # "" — сигнал «пропустить» (mid-run доставка без промпта)
    msg = {"role": "user", "content": "<skill…>", "display_content": ""}
    assert user_display_text(msg) == ""


def test_display_text_falls_back_to_content():
    msg = {"role": "user", "content": "просто запрос"}
    assert user_display_text(msg) == "просто запрос"


def test_display_text_none_for_non_user():
    assert user_display_text({"role": "assistant", "content": "x"}) is None


def test_extract_prefers_display_content():
    msg = {"role": "user", "content": "<skill…>\nдавай", "display_content": "давай"}
    assert extract_real_user_message_text(msg) == "давай"


def test_extract_empty_display_is_empty():
    msg = {"role": "user", "content": "<skill…>", "display_content": ""}
    assert extract_real_user_message_text(msg) == ""


def test_extract_without_display_unchanged():
    assert extract_real_user_message_text({"role": "user", "content": "запрос"}) == "запрос"
