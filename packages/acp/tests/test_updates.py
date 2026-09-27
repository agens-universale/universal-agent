"""kind ядра → session/update и досылка last_answer."""

from gc_acp.updates import ChunkAssembler, history_updates


def test_answer_chunks_share_message_id_think_starts_another():
    assembler = ChunkAssembler()
    first = assembler.feed("answer", "hel")
    second = assembler.feed("answer", "lo")
    thought = assembler.feed("think", "hmm")
    assert first[0]["sessionUpdate"] == "agent_message_chunk"
    assert first[0]["content"] == {"type": "text", "text": "hel"}
    assert second[0]["messageId"] == first[0]["messageId"]
    assert thought[0]["sessionUpdate"] == "agent_thought_chunk"
    assert thought[0]["messageId"] != first[0]["messageId"]


def test_empty_delta_and_tool_json_are_not_forwarded():
    assembler = ChunkAssembler()
    assert assembler.feed("answer", "") == []
    assert assembler.feed("tool", '{"path": "a"}') == []
    assert assembler.feed("reset", "") == []


def test_reset_starts_a_new_message_id_without_a_notification():
    assembler = ChunkAssembler()
    first = assembler.feed("answer", "a")
    assert assembler.feed("reset", "") == []
    again = assembler.feed("answer", "b")
    assert again[0]["messageId"] != first[0]["messageId"]


def test_flush_sends_only_the_unseen_tail():
    assembler = ChunkAssembler()
    assert assembler.flush_answer("только итог")[0]["content"]["text"] == (
        "только итог"
    )
    streamed = ChunkAssembler()
    first = streamed.feed("answer", "ab")
    assert streamed.flush_answer("ab") == []
    tail = streamed.flush_answer("abcd")
    assert tail[0]["content"]["text"] == "cd"
    assert tail[0]["messageId"] == first[0]["messageId"]
    assert tail[0]["sessionUpdate"] == "agent_message_chunk"


def test_flush_after_think_uses_a_new_answer_id():
    assembler = ChunkAssembler()
    thought = assembler.feed("think", "x")
    tail = assembler.flush_answer("ответ")
    assert tail[0]["messageId"] != thought[0]["messageId"]
    assert tail[0]["content"]["text"] == "ответ"


def test_history_skips_tool_role_and_non_text_parts():
    updates = history_updates(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "see"},
                    {"type": "image", "data": "qq"},
                ],
            },
            {"role": "tool", "content": "raw"},
            {"type": "function_call", "name": "read", "arguments": "{}"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "   "},
        ]
    )
    assert [item["sessionUpdate"] for item in updates] == [
        "user_message_chunk",
        "agent_message_chunk",
    ]
    assert updates[0]["content"]["text"] == "see"
    assert updates[1]["content"]["text"] == "done"
    assert updates[0]["messageId"] != updates[1]["messageId"]
