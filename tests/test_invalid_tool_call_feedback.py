from message_policy.user_message_policy import build_invalid_tool_call_feedback_message


def test_large_file_hint_shown_for_fs_write_file():
    msg = {"name": "fs_write_file", "arguments": "{bad json"}
    fb = build_invalid_tool_call_feedback_message(msg)["content"]
    assert "fs_write_file" in fb  # real tool name in hint
    assert "fs_append_file" in fb


def test_large_file_hint_shown_for_fs_append_file():
    msg = {"name": "fs_append_file", "arguments": "{bad json"}
    fb = build_invalid_tool_call_feedback_message(msg)["content"]
    assert "fs_write_file" in fb
    assert "fs_append_file" in fb


def test_no_hint_for_other_tools():
    msg = {"name": "exec_command", "arguments": "{bad json"}
    fb = build_invalid_tool_call_feedback_message(msg)["content"]
    assert "fs_append_file" not in fb
