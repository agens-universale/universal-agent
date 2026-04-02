from user_message_policy import extract_real_user_message_text


def test_extract_real_user_message_text_skips_current_context_as_technical():
    msg = {
        "role": "user",
        "content": 'client has non-empty thread notes; call `thread_notes` with action="read" to load them.',
    }
    out = extract_real_user_message_text(msg)
    assert out == ""


def test_extract_real_user_message_text_skips_validation_error_as_technical():
    msg = {
        "role": "user",
        "content": "❌ Tool Argument Validation Failed\nExpected schema: {'type': 'object'}",
    }
    out = extract_real_user_message_text(msg)
    assert out == ""


def test_extract_real_user_message_text_keeps_regular_user_message():
    msg = {"role": "user", "content": "Сделай рефакторинг parser слоя"}
    out = extract_real_user_message_text(msg)
    assert out == "Сделай рефакторинг parser слоя"
