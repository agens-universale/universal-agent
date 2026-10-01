"""kind ядра → session/update и досылка last_answer."""

import json

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


def test_flush_delivers_short_report_found_inside_earlier_prose():
    assembler = ChunkAssembler()
    earlier = assembler.feed("answer", "Готово, сейчас проверю…")
    report = assembler.flush_answer("Готово")
    assert report[0]["content"]["text"] == "Готово"
    assert report[0]["sessionUpdate"] == "agent_message_chunk"
    assert report[0]["messageId"] != earlier[0]["messageId"]


def test_flush_delivers_report_that_only_appeared_in_an_earlier_message():
    assembler = ChunkAssembler()
    assembler.feed("answer", "Готово, сейчас проверю…")
    assembler.feed("think", "хм")
    assembler.feed("answer", "файл на месте")
    report = assembler.flush_answer("Готово")
    assert report[0]["content"]["text"] == "Готово"


def test_flush_skips_report_equal_to_the_last_message():
    assembler = ChunkAssembler()
    assembler.feed("answer", "Готово, сейчас проверю…")
    assembler.feed("think", "хм")
    assembler.feed("answer", "файл на месте")
    assert assembler.flush_answer("файл на месте") == []


def test_flush_after_think_uses_a_new_answer_id():
    assembler = ChunkAssembler()
    thought = assembler.feed("think", "x")
    tail = assembler.flush_answer("ответ")
    assert tail[0]["messageId"] != thought[0]["messageId"]
    assert tail[0]["content"]["text"] == "ответ"


def test_history_places_a_tool_call_between_prose():
    updates = history_updates(
        [
            {"role": "assistant", "content": "смотрю"},
            {
                "type": "function_call",
                "name": "fs_read_file",
                "call_id": "c1",
                "arguments": '{"path": "src/a.py"}',
            },
            {
                "type": "function_call",
                "name": "turn_finish",
                "call_id": "fin",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": '{"ok": false, "error": "нет файла"}',
            },
            {"role": "assistant", "content": "готово"},
        ]
    )
    assert [item["sessionUpdate"] for item in updates] == [
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
        "agent_message_chunk",
    ]
    assert updates[1]["toolCallId"] == "c1"
    assert updates[1]["status"] == "in_progress"
    assert updates[1]["title"] == "fs_read_file(path='src/a.py')"
    assert updates[2] == {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "c1",
        "status": "failed",
    }


def _finish(call_id, report, **extra):
    payload = {
        "solution_type": "direct_answer",
        "task": "прочитать README",
        "task_report": report,
        "handoff": "{}",
    }
    payload.update(extra)
    return {
        "type": "function_call",
        "name": "turn_finish",
        "call_id": call_id,
        "arguments": json.dumps(payload, ensure_ascii=False),
    }


def test_history_shows_finish_report_and_hides_the_judge():
    updates = history_updates(
        [
            {"role": "user", "content": "прочитай"},
            _finish(
                "bad",
                "черновик",
                comment="рано",
                message="Finish gate: continue_work.",
            ),
            {
                "type": "function_call_output",
                "call_id": "bad",
                "output": json.dumps(
                    {
                        "tool": "turn_finish",
                        "ok": False,
                        "status": "rejected_by_finish_gate",
                        "message": "Finish gate: continue_work.",
                        "finish_gate": {"action": "continue_work"},
                    },
                    ensure_ascii=False,
                ),
            },
            _finish("ok", "В README одна фраза.", comment="файл короткий"),
            {
                "type": "function_call_output",
                "call_id": "ok",
                "output": json.dumps(
                    {
                        "tool": "turn_finish",
                        "ok": True,
                        "status": "submitted",
                        "finish_gate": {"action": "accept"},
                    }
                ),
            },
        ]
    )
    texts = [item["content"]["text"] for item in updates]
    assert texts == [
        "прочитай",
        "В README одна фраза.\n\n* заметка: файл короткий",
    ]
    assert all("Finish gate" not in text for text in texts)
    assert all("finish_gate" not in text for text in texts)


def test_history_skips_finish_report_already_shown_as_prose():
    updates = history_updates(
        [
            {"role": "assistant", "content": "В README одна фраза."},
            _finish("ok", "В README одна фраза.", comment="файл короткий"),
        ]
    )
    assert [item["content"]["text"] for item in updates] == [
        "В README одна фраза."
    ]


def test_history_reads_canonical_output_text():
    updates = history_updates(
        [
            {"role": "user", "content": "прочитай"},
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "annotations": [],
                        "text": "<think>r</think>В README одна фраза.",
                    }
                ],
            },
        ]
    )
    assert [item["sessionUpdate"] for item in updates] == [
        "user_message_chunk",
        "agent_message_chunk",
    ]
    assert updates[1]["content"]["text"] == "В README одна фраза."


def test_history_skips_technical_user_messages():
    updates = history_updates(
        [
            {"role": "user", "content": "прочитай README"},
            {
                "role": "user",
                "content": (
                    '<technical type="dialog_summary">'
                    "<dialog_summary>\n"
                    "<summary>сжатый контекст</summary>\n"
                    "</dialog_summary>"
                    "</technical>"
                ),
            },
            {
                "role": "user",
                "content": (
                    '<technical type="technical">'
                    "Your task checklist (session_todo) has not been updated "
                    "for a while."
                    "</technical>"
                ),
            },
            {
                "role": "user",
                "content": "<skill…>\nдавай обсудим",
                "display_content": "давай обсудим",
            },
            {"role": "assistant", "content": "готово"},
        ]
    )
    texts = [item["content"]["text"] for item in updates]
    assert texts == ["прочитай README", "давай обсудим", "готово"]
    assert [item["sessionUpdate"] for item in updates] == [
        "user_message_chunk",
        "user_message_chunk",
        "agent_message_chunk",
    ]


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


def test_history_shows_subagent_tools_as_agent_actions():
    updates = history_updates(
        [
            {"role": "assistant", "content": "делегирую"},
            {
                "type": "function_call",
                "name": "subagent_explore",
                "call_id": "sub",
                "arguments": '{"task": "найти"}',
            },
            {
                "type": "function_call_output",
                "call_id": "sub",
                "output": '{"ok": true}',
            },
            {"role": "assistant", "content": "готово"},
        ],
        subagent_records=[
            {
                "seq": 1,
                "parent_call_id": "sub",
                "kind": "call",
                "payload": {
                    "type": "function_call",
                    "name": "subagent_inner",
                    "call_id": "inner",
                    "arguments": '{"task": "глубже"}',
                },
            },
            {
                "seq": 2,
                "parent_call_id": "inner",
                "kind": "call",
                "payload": {
                    "type": "function_call",
                    "name": "fs_read_file",
                    "call_id": "c1",
                    "arguments": '{"path": "a.py"}',
                },
            },
            {
                "seq": 3,
                "parent_call_id": "inner",
                "kind": "error",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": '{"ok": false}',
                },
            },
            {
                "seq": 4,
                "parent_call_id": "sub",
                "kind": "result",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "inner",
                    "output": '{"ok": true}',
                },
            },
            {
                "seq": 5,
                "parent_call_id": "sub",
                "kind": "intermediate_reply",
                "payload": {"prose": "ещё смотрю"},
            },
        ],
    )
    assert [
        (item["sessionUpdate"], item.get("toolCallId"), item.get("status"))
        for item in updates
    ] == [
        ("agent_message_chunk", None, None),
        ("tool_call", "sub", "in_progress"),
        ("tool_call", "sub:inner", "in_progress"),
        ("tool_call", "inner:c1", "in_progress"),
        ("tool_call_update", "inner:c1", "failed"),
        ("tool_call_update", "sub:inner", "completed"),
        ("tool_call_update", "sub", "completed"),
        ("agent_message_chunk", None, None),
    ]
    assert updates[3]["title"] == "fs_read_file(path='a.py')"


def test_history_drops_subagent_steps_after_compaction():
    updates = history_updates(
        [
            {
                "role": "user",
                "content": (
                    '<technical type="dialog_summary">'
                    "<dialog_summary>\n"
                    "<summary>сжатый контекст</summary>\n"
                    "</dialog_summary>"
                    "</technical>"
                ),
            },
            {"role": "user", "content": "ещё раз"},
            {"role": "assistant", "content": "готово"},
        ],
        subagent_records=[
            {
                "seq": 1,
                "parent_call_id": "gone",
                "kind": "call",
                "payload": {
                    "type": "function_call",
                    "name": "fs_read_file",
                    "call_id": "c1",
                    "arguments": '{"path": "a.py"}',
                },
            },
            {
                "seq": 2,
                "parent_call_id": "gone",
                "kind": "result",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": '{"ok": true}',
                },
            },
            {
                "seq": 3,
                "parent_call_id": "gone",
                "kind": "call",
                "payload": {
                    "type": "function_call",
                    "name": "fs_read_file",
                    "call_id": "c2",
                    "arguments": '{"path": "b.py"}',
                },
            },
        ],
    )
    assert [item["sessionUpdate"] for item in updates] == [
        "user_message_chunk",
        "agent_message_chunk",
    ]
    assert [item["content"]["text"] for item in updates] == [
        "ещё раз",
        "готово",
    ]
