from message_policy.chat_completions import repair_tool_call_pairing


def test_repair_tool_call_pairing_keeps_only_answered_calls():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_ok",
                    "type": "function",
                    "function": {"name": "x", "arguments": "{}"},
                },
                {
                    "id": "call_missing",
                    "type": "function",
                    "function": {"name": "y", "arguments": "{}"},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_ok",
            "content": '{"ok":true}',
        },
    ]

    repaired = repair_tool_call_pairing(messages)
    assert len(repaired) == 2
    assert repaired[0]["role"] == "assistant"
    assert len(repaired[0]["tool_calls"]) == 1
    assert repaired[0]["tool_calls"][0]["id"] == "call_ok"


def test_repair_tool_call_pairing_keeps_latest_unanswered_call():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_new",
                    "type": "function",
                    "function": {"name": "x", "arguments": "{}"},
                }
            ],
        }
    ]

    repaired = repair_tool_call_pairing(messages)
    assert repaired == messages


def test_repair_tool_call_pairing_drops_latest_unanswered_call_in_strict_mode():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_new",
                    "type": "function",
                    "function": {"name": "x", "arguments": "{}"},
                }
            ],
        }
    ]

    repaired = repair_tool_call_pairing(messages, keep_latest_unanswered=False)
    assert repaired == []


def test_repair_tool_call_pairing_handles_canonical_function_call_history():
    messages = [
        {
            "id": "fc_ok",
            "type": "function_call",
            "call_id": "call_ok",
            "name": "tool_a",
            "arguments": "{}",
            "status": "completed",
        },
        {
            "id": "fc_missing",
            "type": "function_call",
            "call_id": "call_missing",
            "name": "tool_b",
            "arguments": "{}",
            "status": "completed",
        },
        {
            "id": "fco_ok",
            "type": "function_call_output",
            "call_id": "call_ok",
            "output": "{\"ok\": true}",
        },
    ]

    repaired = repair_tool_call_pairing(messages)
    assert len(repaired) == 2
    assert repaired[0]["type"] == "function_call"
    assert repaired[0]["call_id"] == "call_ok"
    assert repaired[1]["type"] == "function_call_output"
    assert repaired[1]["call_id"] == "call_ok"
