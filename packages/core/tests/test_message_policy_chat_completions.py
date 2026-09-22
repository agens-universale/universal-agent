from gc_core.message_policy.chat_completions import (
    coalesce_consecutive_assistant_tool_calls,
    inject_assistant_after_orphan_tool_outputs,
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


def test_coalesce_keeps_reasoning_content_from_leading_prose():
    messages = [
        {
            "role": "assistant",
            "content": "planning",
            "reasoning_content": "secret",
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {"name": "x", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
    ]
    coalesced = coalesce_consecutive_assistant_tool_calls(messages)
    assert coalesced[0]["reasoning_content"] == "secret"
    assert coalesced[0]["content"] == "planning"
    assert len(coalesced[0]["tool_calls"]) == 1


def test_prepare_keeps_reasoning_content_after_coalesce():
    messages = [
        {
            "role": "assistant",
            "content": "planning",
            "reasoning_content": "secret",
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {"name": "x", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
    ]
    prepared = prepare_chat_completion_messages_for_api(messages)
    assistants = [m for m in prepared if m.get("role") == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["reasoning_content"] == "secret"
    assert assistants[0]["content"] == "planning"


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


def test_inject_assistant_after_tool_followed_by_user_inserts_synthetic_ack():
    """Stop посреди агентского хода: tool → user без assistant между → вставка ack."""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "m", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "user", "content": "продолжи"},
    ]
    out = inject_assistant_after_orphan_tool_outputs(messages)
    assert len(out) == 4  # вставлен синтетический assistant
    assert out[0]["role"] == "assistant" and out[0].get("tool_calls")
    assert out[1]["role"] == "tool"
    assert out[2]["role"] == "assistant" and not out[2].get("tool_calls")
    assert out[2]["content"]  # placeholder есть
    assert out[3]["role"] == "user"


def test_inject_assistant_handles_multiple_tool_messages_in_series():
    """Серия tool-сообщений (несколько call'ов в одном assistant) — ack один."""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "m", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "s", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
        {"role": "user", "content": "продолжи"},
    ]
    out = inject_assistant_after_orphan_tool_outputs(messages)
    # 4 исходных + 1 synthetic перед последним user
    assert len(out) == 5
    assert out[-2]["role"] == "assistant" and not out[-2].get("tool_calls")
    assert out[-1]["role"] == "user"


def test_inject_assistant_inert_when_tool_followed_by_assistant():
    """Здоровый поток tool → assistant — вставки нет."""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "m", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "финальный ответ"},
        {"role": "user", "content": "следующий вопрос"},
    ]
    out = inject_assistant_after_orphan_tool_outputs(messages)
    assert len(out) == 4  # без вставок
    assert [m["role"] for m in out] == ["assistant", "tool", "assistant", "user"]


def test_inject_assistant_inert_on_empty_or_no_tool_messages():
    """Empty + no-tool history — функция возвращает как есть."""
    assert inject_assistant_after_orphan_tool_outputs([]) == []
    plain = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "more"},
    ]
    out = inject_assistant_after_orphan_tool_outputs(plain)
    assert out == plain


def test_inject_assistant_inert_on_consecutive_user_messages():
    """user → user → user legitimate в проекте (см. create_call_data_completions
    тест test_create_call_data_completions_handles_messages_without_id) —
    injector не должен туда вмешиваться."""
    messages = [
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "продолжи"},
        {"role": "user", "content": "продолжи"},
        {"role": "user", "content": "ты здесь?"},
    ]
    out = inject_assistant_after_orphan_tool_outputs(messages)
    assert out == messages  # без модификаций


def test_inject_assistant_before_system_after_tool():
    """tool → system — тоже вставка ack ([policy] feedback message и т.п.)."""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "m", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "system", "content": "[policy] something"},
    ]
    out = inject_assistant_after_orphan_tool_outputs(messages)
    assert len(out) == 4
    assert out[2]["role"] == "assistant"
    assert out[3]["role"] == "system"


def test_prepare_chat_completion_messages_repairs_stop_corrupted_history():
    """End-to-end: prepare фиксит history где stop оставил tool→user без assistant.

    Это ровно паттерн из bug report'а (thread d37cc345):
      assistant(text) → function_call → tool → tool → user
    """
    messages = [
        {"role": "user", "content": "расскажи о проекте"},
        {"role": "assistant", "content": "думаю..."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "long_memory", "arguments": "{}"}},
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "memory data"},
        {"role": "tool", "tool_call_id": "c2", "content": "file data"},
        # STOP HERE — assistant final response не пришёл
        {"role": "user", "content": "продолжи"},
    ]
    prepared = prepare_chat_completion_messages_for_api(messages)
    roles = [m["role"] for m in prepared]
    # После tool серии должен быть assistant (ack), потом user
    assert "assistant" in roles
    user_idx = roles.index("user", 1)  # пропускаем первый user
    assert roles[user_idx - 1] == "assistant"  # перед последним user — assistant
