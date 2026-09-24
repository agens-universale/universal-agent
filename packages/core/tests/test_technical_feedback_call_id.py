from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_ERROR_MESSAGE,
)
from gc_core.processing_responses import _make_technical_feedback_user_message


def test_call_id_attached_as_sibling_key():
    msg = _make_technical_feedback_user_message(
        "boom",
        technical_type=TECHNICAL_TYPE_ERROR_MESSAGE,
        call_id="call_42",
    )
    assert msg["role"] == "user"
    assert msg["call_id"] == "call_42"
    assert msg["content"].startswith(
        '<technical type="error_message">'
    )  # текст не тронут


def test_no_call_id_means_no_key():
    msg = _make_technical_feedback_user_message("boom")
    assert "call_id" not in msg
