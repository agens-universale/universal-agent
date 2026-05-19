from message_policy.chat_completions import (
    coalesce_consecutive_assistant_tool_calls,
    prepare_chat_completion_messages_for_api,
    repair_tool_call_pairing,
)


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


def test_coalesce_consecutive_assistant_tool_calls_merges_parallel_batch():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b", "type": "function", "function": {"name": "y", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "content": "2"},
    ]
    coalesced = coalesce_consecutive_assistant_tool_calls(messages)
    assert len(coalesced) == 3
    assert coalesced[0]["role"] == "assistant"
    assert len(coalesced[0]["tool_calls"]) == 2
    assert {tc["id"] for tc in coalesced[0]["tool_calls"]} == {"a", "b"}


def test_coalesce_merges_leading_assistant_text_before_tool_calls():
    messages = [
        {"role": "assistant", "content": "planning", "tool_calls": []},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
    ]
    coalesced = coalesce_consecutive_assistant_tool_calls(messages)
    assert len(coalesced) == 2
    assert coalesced[0]["content"] == "planning"
    assert len(coalesced[0]["tool_calls"]) == 1


def test_prepare_chat_completion_messages_for_api_parallel_planner_shape():
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "m", "arguments": "{}"}}]},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "s", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
    ]
    prepared = prepare_chat_completion_messages_for_api(messages)
    assistants = [m for m in prepared if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistants) == 1
    assert len(assistants[0]["tool_calls"]) == 2
