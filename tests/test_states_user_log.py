from user_message_policy import to_user_log_entry


def test_to_user_log_entry_marks_current_context_as_technical():
    msg = {
        "role": "user",
        "content": 'client has non-empty thread notes; call `thread_notes` with action="read" to load them.',
    }
    out = to_user_log_entry(msg)
    assert out.startswith("[user][technical]")
    assert '<technical type="current_context">' in out


def test_to_user_log_entry_marks_validation_error_as_error_message():
    msg = {
        "role": "user",
        "content": "❌ Tool Argument Validation Failed\nExpected schema: {'type': 'object'}",
    }
    out = to_user_log_entry(msg)
    assert out.startswith("[user][technical]")
    assert '<technical type="error_message">' in out


def test_to_user_log_entry_keeps_regular_user_message():
    msg = {"role": "user", "content": "Сделай рефакторинг parser слоя"}
    out = to_user_log_entry(msg)
    assert out == "[user] Сделай рефакторинг parser слоя"
