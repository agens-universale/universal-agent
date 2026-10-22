"""kind ядра → session/update и досылка last_answer."""

import json
import os

from gc_acp.updates import ChunkAssembler, history_updates
from gc_core.message_policy.plan_messages import accepted_plan_messages


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


def test_reply_shown_resets_on_reset():
    assembler = ChunkAssembler()
    assert assembler.reply_shown is False
    assembler.feed("think", "хм")
    assert assembler.reply_shown is False
    assembler.feed("answer", "Пока!")
    assert assembler.reply_shown is True
    assembler.feed("reset", "")
    assert assembler.reply_shown is False


def test_append_answer_is_a_new_message_as_is():
    assembler = ChunkAssembler()
    first = assembler.feed("answer", "Пока!")
    record = assembler.append_answer("\n\n* **итог:**\n  х")
    assert record[0]["sessionUpdate"] == "agent_message_chunk"
    assert record[0]["content"]["text"] == "\n\n* **итог:**\n  х"
    assert record[0]["messageId"] != first[0]["messageId"]
    assert assembler.append_answer("") == []


def test_append_answer_after_earlier_answer_gets_blank_line():
    assembler = ChunkAssembler()
    assert assembler.append_answer("Первое")[0]["content"]["text"] == (
        "Первое"
    )
    assembler = ChunkAssembler()
    assembler.feed("answer", "Сделал.")
    assembler.feed("reset", "")
    record = assembler.append_answer("Сделал.")
    assert record[0]["content"]["text"] == "\n\nСделал."


def test_new_answer_after_earlier_answer_gets_blank_line():
    """Клиент склеивает прозу хода в один блок: ответ модели после тулов
    отделяется пустой строкой. Первый ответ прогона — без неё."""
    assembler = ChunkAssembler()
    first = assembler.feed("answer", "Сейчас посмотрю README.")
    assembler.feed("reset", "")
    second = assembler.feed("answer", "Проект — песочница.")
    tail = assembler.feed("answer", " Тесты есть.")
    assert first[0]["content"]["text"] == "Сейчас посмотрю README."
    assert second[0]["content"]["text"] == "\n\nПроект — песочница."
    assert tail[0]["content"]["text"] == " Тесты есть."
    assert assembler.last_answer_text == "Проект — песочница. Тесты есть."


def test_think_inside_one_response_does_not_split_the_answer():
    """Мысль внутри одного ответа модели не рвёт фразу пустой строкой."""
    assembler = ChunkAssembler()
    assembler.feed("answer", "Сделал X и")
    assembler.feed("think", "хм")
    tail = assembler.feed("answer", " Y.")
    assert tail[0]["content"]["text"] == " Y."
    assert assembler.last_answer_text == "Сделал X и Y."
    assert assembler.flush_answer("Сделал X и Y.") == []


def test_next_response_after_reset_and_think_gets_blank_line():
    assembler = ChunkAssembler()
    assembler.feed("answer", "A")
    assembler.feed("reset", "")
    assembler.feed("think", "хм")
    second = assembler.feed("answer", "B")
    assert second[0]["content"]["text"] == "\n\nB"
    assert assembler.last_answer_text == "B"


def test_new_answer_starting_with_newline_is_not_padded():
    assembler = ChunkAssembler()
    assembler.feed("answer", "a")
    assembler.feed("reset", "")
    assert assembler.feed("answer", "\nb")[0]["content"]["text"] == "\nb"


def test_answer_after_think_without_earlier_answer_is_not_padded():
    assembler = ChunkAssembler()
    assembler.feed("think", "хм")
    first = assembler.feed("answer", "ответ")
    assert first[0]["content"]["text"] == "ответ"


def test_next_run_of_the_same_prompt_keeps_the_blank_line():
    """Продолжение после одобрения плана — новый прогон того же хода:
    его первая реплика тоже отделяется от уже показанной прозы."""
    first = ChunkAssembler()
    assert first.prose_sent is False
    first.append_answer("1. План")
    assert first.prose_sent is True
    after_plan = ChunkAssembler(prose_sent=first.prose_sent)
    assert after_plan.feed("answer", "Готово.")[0]["content"]["text"] == (
        "\n\nГотово."
    )


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
    assert report[0]["content"]["text"] == "\n\nГотово"
    assert report[0]["sessionUpdate"] == "agent_message_chunk"
    assert report[0]["messageId"] != earlier[0]["messageId"]


def test_flush_delivers_report_that_only_appeared_in_an_earlier_message():
    assembler = ChunkAssembler()
    assembler.feed("answer", "Готово, сейчас проверю…")
    assembler.feed("reset", "")
    assembler.feed("think", "хм")
    assembler.feed("answer", "файл на месте")
    report = assembler.flush_answer("Готово")
    assert report[0]["content"]["text"] == "\n\nГотово"


def test_flush_skips_report_equal_to_the_last_message():
    assembler = ChunkAssembler()
    assembler.feed("answer", "Готово, сейчас проверю…")
    assembler.feed("reset", "")
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
    assert updates[1]["title"] == "fs_read_file(src/a.py)"
    assert updates[1]["name"] == "fs_read_file"
    assert updates[1]["rawInput"] == {"path": "src/a.py"}
    assert updates[1]["locations"] == [{"path": os.path.abspath("src/a.py")}]
    assert "content" not in updates[1]
    assert "kind" not in updates[1]
    assert updates[2] == {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "c1",
        "status": "failed",
        "content": [
            {
                "type": "content",
                "content": {
                    "type": "text",
                    "text": '{"ok":false,"error":"нет файла"}',
                },
            }
        ],
    }
    assert "rawOutput" not in updates[2]
    assert "locations" not in updates[2]


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


def _message(text, role="assistant"):
    return {
        "type": "message",
        "role": role,
        "content": [{"type": "output_text", "text": text}],
    }


def _finish_ok(call_id):
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps({"tool": "turn_finish", "ok": True}),
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
        "В README одна фраза.\n\n* **заметка:**\n\n  файл короткий",
    ]
    assert all("Finish gate" not in text for text in texts)
    assert all("finish_gate" not in text for text in texts)


def test_history_report_equal_to_reply_keeps_only_the_note():
    updates = history_updates(
        [
            _message("В README одна фраза."),
            _finish("ok", "В README одна фраза.", comment="файл короткий"),
        ]
    )
    assert [item["content"]["text"] for item in updates] == [
        "В README одна фраза.",
        "\n\n* **заметка:**\n\n  файл короткий",
    ]


def test_history_reply_and_report_render_summary_chunk():
    updates = history_updates(
        [
            _message("прощайся", "user"),
            _message("Пока! 👋"),
            _finish("ok", "Пользователь попросил попрощаться."),
            _finish_ok("ok"),
        ]
    )
    assert [item["content"]["text"] for item in updates] == [
        "прощайся",
        "Пока! 👋",
        "\n\n* **итог:**\n\n  Пользователь попросил попрощаться.",
    ]
    assert len({item["messageId"] for item in updates}) == 3


def test_history_list_report_and_comment_are_paragraphs():
    """Оговорка после отчёта-списка — свой абзац записи, не пункт."""
    updates = history_updates(
        [
            _message("Готово."),
            _finish("ok", "- a\n- b", comment="Риск."),
            _finish_ok("ok"),
        ]
    )
    assert [item["content"]["text"] for item in updates] == [
        "Готово.",
        "\n\n* **итог:**\n\n  - a\n  - b\n\n  Риск.",
    ]


def test_history_report_without_reply_is_answer():
    updates = history_updates(
        [
            _message("прочитай", "user"),
            _finish("ok", "В README одна фраза."),
            _finish_ok("ok"),
        ]
    )
    assert [item["content"]["text"] for item in updates] == [
        "прочитай",
        "В README одна фраза.",
    ]


def test_history_report_equal_to_previous_turn_prose_is_shown():
    """Проза прошлого хода не глушит отчёт этого: «тот же ответ» — только
    записи того же ответа модели."""
    updates = history_updates(
        [
            _message("привет", "user"),
            _message("Готово."),
            {"role": "user", "content": "ещё раз", "id": "m_2"},
            _finish("ok", "Готово."),
            _finish_ok("ok"),
        ]
    )
    assert [item["content"]["text"] for item in updates] == [
        "привет",
        "Готово.",
        "ещё раз",
        "Готово.",
    ]


def test_history_report_equal_to_prose_before_technical_bounce_not_repeated():
    """Служебное сообщение ядра лежит в треде как user без type и отделяет
    ответы модели; отчёт, равный уже показанной в этом ходе прозе, не
    повторяется."""
    bounce = {
        "role": "user",
        "content": (
            '<technical type="technical">There is no tool call in your '
            "response.</technical>"
        ),
        "id": "m_b",
    }
    updates = history_updates(
        [
            _message("сделай", "user"),
            _message("Сделал."),
            bounce,
            _finish("ok", "Сделал."),
            _finish_ok("ok"),
        ]
    )
    assert [item["content"]["text"] for item in updates] == [
        "сделай",
        "Сделал.",
    ]


def test_history_bounced_reply_with_comment_keeps_only_the_note():
    bounce = {
        "role": "user",
        "content": (
            '<technical type="technical">There is no tool call in your '
            "response.</technical>"
        ),
        "id": "m_b",
    }
    updates = history_updates(
        [
            _message("сделай", "user"),
            _message("Сделал."),
            bounce,
            _finish("ok", "Сделал.", comment="проверил"),
            _finish_ok("ok"),
        ]
    )
    assert [item["content"]["text"] for item in updates] == [
        "сделай",
        "Сделал.",
        "\n\n* **заметка:**\n\n  проверил",
    ]


def test_history_plan_after_reply_is_separate_paragraph():
    plan = "1. Добавить тест\n2. Починить"
    updates = history_updates(
        [
            _message("спланируй", "user"),
            _message("Вот план."),
            _propose("p1", plan),
            _plan_output("p1", ok=True),
        ]
    )
    assert [item["content"]["text"] for item in updates] == [
        "спланируй",
        "Вот план.",
        "\n\n" + plan,
    ]


def test_history_prose_after_tools_is_separate_paragraph():
    """Как в живом ходе: ответ модели после тулов — с пустой строкой,
    запись конца хода — ровно с одной."""
    updates = history_updates(
        [
            _message("о чём проект?", "user"),
            _message("Сейчас посмотрю README."),
            {
                "type": "function_call",
                "name": "fs_read_file",
                "call_id": "c1",
                "arguments": '{"path": "README.md"}',
            },
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": '{"ok": true}',
            },
            _message("Проект — песочница."),
            _finish("ok", "Проект — песочница.", comment="проверил"),
            _finish_ok("ok"),
            _message("а тесты?", "user"),
            _message("Есть."),
        ]
    )
    assert [
        item["content"]["text"]
        for item in updates
        if not item["sessionUpdate"].startswith("tool_call")
    ] == [
        "о чём проект?",
        "Сейчас посмотрю README.",
        "\n\nПроект — песочница.",
        "\n\n* **заметка:**\n\n  проверил",
        "а тесты?",
        "Есть.",
    ]


def test_history_reply_after_approved_plan_is_separate_paragraph():
    """Как в живом продолжении: служебное «план одобрен» ход не делит."""
    plan = "1. Добавить тест\n2. Починить"
    (accepted,) = accepted_plan_messages("edit")
    updates = history_updates(
        [
            _message("спланируй", "user"),
            _propose("p1", plan),
            _plan_output("p1", ok=True),
            {"role": "user", "content": accepted},
            _message("Готово."),
        ]
    )
    assert [item["content"]["text"] for item in updates] == [
        "спланируй",
        plan,
        "\n\nГотово.",
    ]


def test_history_rejected_finish_hidden():
    updates = history_updates(
        [
            _message("Пока! 👋"),
            _finish("bad", "черновик"),
            {
                "type": "function_call_output",
                "call_id": "bad",
                "output": json.dumps({"tool": "turn_finish", "ok": False}),
            },
        ]
    )
    assert [item["content"]["text"] for item in updates] == ["Пока! 👋"]


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
    assert updates[3]["title"] == "fs_read_file(a.py)"
    assert updates[3]["name"] == "fs_read_file"
    assert updates[3]["rawInput"] == {"path": "a.py"}
    assert updates[3]["locations"] == [{"path": os.path.abspath("a.py")}]
    failed = updates[4]
    assert failed["content"][0]["content"]["text"] == '{"ok":false}'
    assert "rawOutput" not in failed
    assert "locations" not in failed


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


def test_history_replay_does_not_rebuild_a_diff_from_disk(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("уже другой текст\n", encoding="utf-8")
    updates = history_updates(
        [
            {
                "type": "function_call",
                "name": "fs_write_file",
                "call_id": "w1",
                "arguments": json.dumps({"path": str(target)}),
            },
            {
                "type": "function_call_output",
                "call_id": "w1",
                "output": json.dumps({"ok": True}),
            },
        ]
    )
    assert [item["sessionUpdate"] for item in updates] == [
        "tool_call",
        "tool_call_update",
    ]
    assert updates[0]["name"] == "fs_write_file"
    assert updates[0]["rawInput"] == {"path": str(target)}
    assert updates[0]["locations"] == [{"path": os.path.abspath(str(target))}]
    assert updates[1]["status"] == "completed"
    assert updates[1]["content"] == [
        {
            "type": "content",
            "content": {"type": "text", "text": '{"ok":true}'},
        }
    ]
    assert "rawOutput" not in updates[1]


def _propose(call_id, plan, **extra):
    return {
        "type": "function_call",
        "name": "turn_propose_plan",
        "call_id": call_id,
        "arguments": json.dumps({"plan": plan, **extra}, ensure_ascii=False),
    }


def _plan_output(call_id, *, ok):
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(
            {
                "tool": "turn_propose_plan",
                "ok": ok,
                "status": "proposed" if ok else "rejected",
            }
        ),
    }


def test_history_shows_the_whole_plan_as_text():
    """Карточка тула урезала бы план до 5000 знаков; план до 16000 —
    показывается целиком, текстом, один раз."""
    plan = "# План\n" + "\n".join(
        f"{n}. шаг {'x' * 80}" for n in range(1, 120)
    )
    assert len(plan) > 5000
    updates = history_updates(
        [
            {"role": "user", "content": "спланируй"},
            _propose("p1", plan),
            _plan_output("p1", ok=True),
        ]
    )
    assert [item["sessionUpdate"] for item in updates] == [
        "user_message_chunk",
        "agent_message_chunk",
    ]
    assert updates[1]["content"]["text"] == plan


def test_history_skips_a_rejected_plan_and_a_repeat_show():
    """Отклонённый тулом план и повторный показ (show_current) в ленту не
    идут: первый не был предложен, второй уже стоит в истории."""
    updates = history_updates(
        [
            _propose("bad", "слишком рано"),
            _plan_output("bad", ok=False),
            _propose("again", "", show_current=True),
            _plan_output("again", ok=True),
        ]
    )
    assert updates == []


def test_history_title_path_is_relative_to_the_session_cwd():
    """Проигрыш — те же подписи, что живой поток: путь от корня проекта."""
    call = {
        "type": "function_call",
        "name": "fs_read_file",
        "call_id": "c1",
        "arguments": '{"path": "/work/app/src/a.py"}',
    }
    updates = history_updates(
        [{**call, "name": "subagent_explore", "arguments": "{}"}],
        subagent_records=[
            {"seq": 1, "parent_call_id": "c1", "kind": "call", "payload": call}
        ],
        root="/work/app",
    )
    assert updates[1]["title"] == "fs_read_file(src/a.py)"


def _replay_of_thread(threads_dir, *, with_outcome: bool) -> list[dict]:
    """Проигрыш ACP с диска: тот же загрузчик, что у ``ThreadSession``."""
    from gc_core.persistence.turn_outcome import TurnOutcome
    from gc_core.thread_store import (
        append_new_thread_messages,
        append_turn_outcome_record,
        load_thread_messages,
    )
    from gc_acp.session_index import ACP_SYSTEM

    turns = [
        [
            {"role": "user", "content": "первый", "id": "u1"},
            {"role": "assistant", "content": "ответ раз", "id": "a1"},
        ],
        [
            {"role": "user", "content": "второй", "id": "u2"},
            {"role": "assistant", "content": "ответ два", "id": "a2"},
        ],
    ]
    seq = 0
    for turn in turns:
        append_new_thread_messages(
            str(threads_dir), ACP_SYSTEM, "s1", turn, set(), start_seq=seq
        )
        seq += len(turn)
        if with_outcome:
            append_turn_outcome_record(
                str(threads_dir),
                ACP_SYSTEM,
                "s1",
                TurnOutcome(
                    status="done",
                    reason=None,
                    opened_by=turn[0]["id"],
                    started_at="2026-10-22T11:32:00.000Z",
                    ended_at="2026-10-22T11:32:12.000Z",
                    duration_sec=12.0,
                    error=None,
                ),
                seq=seq,
            )
    messages, _keys, _seq = load_thread_messages(
        str(threads_dir), ACP_SYSTEM, "s1"
    )
    # messageId — случайный на каждый проигрыш, сравнивается остальное.
    return [
        {k: v for k, v in update.items() if k != "messageId"}
        for update in history_updates(messages)
    ]


def test_history_ignores_turn_outcome_records(tmp_path):
    """ACP строку итога хода не показывает: проигрыш треда с записями
    итога ядра — тот же, что без них."""
    plain = _replay_of_thread(tmp_path / "plain", with_outcome=False)
    recorded = _replay_of_thread(tmp_path / "rec", with_outcome=True)

    assert recorded == plain
    assert plain
