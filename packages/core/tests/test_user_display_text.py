from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)


def test_extract_prefers_display_content():
    msg = {
        "role": "user",
        "content": "<skill…>\nдавай",
        "display_content": "давай",
    }
    assert extract_real_user_message_text(msg) == "давай"


def test_extract_empty_display_is_empty():
    msg = {"role": "user", "content": "<skill…>", "display_content": ""}
    assert extract_real_user_message_text(msg) == ""


def test_extract_without_display_unchanged():
    assert (
        extract_real_user_message_text({"role": "user", "content": "запрос"})
        == "запрос"
    )


def test_display_content_none_falls_back_to_content():
    msg = {"role": "user", "content": "запрос", "display_content": None}
    assert extract_real_user_message_text(msg) == "запрос"
