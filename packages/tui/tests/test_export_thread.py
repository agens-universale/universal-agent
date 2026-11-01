"""export_thread — json (модель segments[]) и txt (аннотированный лог + сводки)."""

import json

from gc_tui.services.export import export_thread


def _env(seq, ts, payload):
    return {
        "type": "message",
        "thread_id": "tid1",
        "system": "cli",
        "seq": seq,
        "ts": ts,
        "payload": payload,
    }


def _segs(records, synthesis=None, n=0):
    """Один сегмент из envelope-записей (опц. compacted_by)."""
    comp = (
        None
        if synthesis is None
        else {"ts": "t", "messages_in_segment": n, "synthesis": synthesis}
    )
    return [{"index": 0, "messages": records, "compacted_by": comp}]


def _seg(payloads, synthesis=None, n=0, index=0):
    """Сегмент из сырых payload'ов (для multi-segment тестов)."""
    comp = (
        None
        if synthesis is None
        else {"ts": "t", "messages_in_segment": n, "synthesis": synthesis}
    )
    return {
        "index": index,
        "messages": [{"payload": p, "ts": "t"} for p in payloads],
        "compacted_by": comp,
    }


def _sample_records():
    return [
        _env(
            1,
            "2026-05-30T10:12:03Z",
            {"role": "user", "content": "вопрос", "id": "m_u1"},
        ),
        _env(
            2,
            "2026-05-30T10:18:44Z",
            {
                "id": "msg_a1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "ответ", "annotations": []}
                ],
            },
        ),
    ]


def test_json_wrapper_shape():
    out = export_thread(
        _segs(_sample_records()),
        fmt="json",
        api_mode="chat_completions",
        thread_id="tid1",
        system="cli",
    )
    doc = json.loads(out)

    assert set(doc.keys()) == {"thread", "segments"}
    assert doc["thread"]["thread_id"] == "tid1"
    assert doc["thread"]["system"] == "cli"
    assert doc["thread"]["backend_api_mode"] == "chat_completions"
    assert doc["thread"]["messages_count"] == 2
    assert doc["thread"]["ts_first"] == "2026-05-30T10:12:03Z"
    assert doc["thread"]["ts_last"] == "2026-05-30T10:18:44Z"
    assert doc["thread"]["summarizations"] == 0
    # segments[0].messages — сырые payload'ы.
    assert doc["segments"][0]["messages"][0]["content"] == "вопрос"
    assert doc["segments"][0]["messages"][1]["content"][0]["text"] == "ответ"
    assert doc["segments"][0]["compacted_by"] is None


def test_json_segments_model():
    segments = [
        _seg(
            [{"role": "user", "content": "вопрос-0"}],
            synthesis="СИНТЕЗ-1",
            n=1,
        ),
        _seg([{"role": "user", "content": "вопрос-1"}], index=1),
    ]
    out = export_thread(
        segments, fmt="json", api_mode="", thread_id="t1", system="cli"
    )
    data = json.loads(out)
    assert data["thread"]["summarizations"] == 1
    assert len(data["segments"]) == 2
    assert data["segments"][0]["messages"][0]["content"] == "вопрос-0"
    assert data["segments"][0]["compacted_by"]["synthesis"] == "СИНТЕЗ-1"
    assert data["segments"][1]["compacted_by"] is None
    assert "messages" not in data  # модель segments, не плоский messages


def test_json_empty_records():
    out = export_thread(
        [], fmt="json", api_mode="", thread_id="tid1", system="cli"
    )
    doc = json.loads(out)
    assert doc["segments"] == []
    assert doc["thread"]["messages_count"] == 0


def _txt(records, api_mode=""):
    return export_thread(
        _segs(records),
        fmt="txt",
        api_mode=api_mode,
        thread_id="tid1",
        system="cli",
    )


def test_txt_header_and_numbering():
    out = _txt(_sample_records(), api_mode="chat_completions")
    assert out.startswith("===== Thread tid1")
    assert "2 messages" in out
    assert "chat_completions" in out
    assert "[1] USER" in out
    assert "вопрос" in out
    assert "[2] ASSISTANT" in out
    assert "ответ" in out


def test_txt_has_inline_summary_block():
    segments = [
        _seg(
            [{"role": "user", "content": "вопрос-0"}],
            synthesis="СИНТЕЗ-1",
            n=1,
        ),
        _seg([{"role": "user", "content": "вопрос-1"}], index=1),
    ]
    out = export_thread(
        segments,
        fmt="txt",
        api_mode="",
        thread_id="t1",
        system="cli",
    )
    assert "вопрос-0" in out
    assert "КОНТЕКСТ СЖАТ #1" in out
    assert "СИНТЕЗ-1" in out
    assert "вопрос-1" in out
    assert "summarized ×1" in out


def test_txt_assistant_think_split():
    records = [
        _env(
            1,
            "t",
            {
                "id": "msg_a",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "<think>обдумываю структуру</think>финальный ответ",
                        "annotations": [],
                    }
                ],
            },
        )
    ]
    out = _txt(records)
    assert "think:" in out
    assert "обдумываю структуру" in out
    assert "финальный ответ" in out
    # think идёт раньше ответа.
    assert out.index("обдумываю структуру") < out.index("финальный ответ")


def test_txt_call_output_pairing_and_full_content():
    records = [
        _env(
            1,
            "t",
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "tool-abc",
                "name": "read_file",
                "arguments": '{"path": "src/cli.py"}',
                "status": "completed",
            },
        ),
        _env(
            2,
            "t",
            {
                "id": "fco_1",
                "type": "function_call_output",
                "call_id": "tool-abc",
                "output": '{"ok": true, "total_lines": 312, "content": "ПОЛНЫЙ-ТЕКСТ"}',
            },
        ),
    ]
    out = _txt(records)
    assert "[1] CALL read_file (call_id=tool-abc)" in out
    # double-encoded arguments распарсены в pretty.
    assert '"path": "src/cli.py"' in out
    # OUTPUT восстановил имя тула по call_id.
    assert "[2] OUTPUT read_file (call_id=tool-abc)" in out
    # полный output, не обрезан.
    assert "ПОЛНЫЙ-ТЕКСТ" in out


def test_txt_technical_user_message_labeled():
    from gc_core.message_policy.user_message_policy import (
        TECHNICAL_TYPE_TECHNICAL,
        wrap_technical_user_content,
    )

    records = [
        _env(
            1,
            "t",
            {
                "role": "user",
                "id": "m_t",
                "content": wrap_technical_user_content(
                    "служебная заметка",
                    technical_type=TECHNICAL_TYPE_TECHNICAL,
                ),
            },
        )
    ]
    out = _txt(records)
    assert "[1] USER [technical:technical]" in out
    assert "служебная заметка" in out


def test_txt_empty_records():
    out = _txt([])
    assert "(пусто)" in out


def _thought_env(text, after):
    from gc_core.persistence.reasoning_record import (
        THOUGHT_TYPE,
        ReasoningRecord,
        reasoning_entry,
    )

    entry = reasoning_entry(ReasoningRecord(text, 1.5, None), after=after)
    return {"type": THOUGHT_TYPE, "seq": 1, "ts": "t", **entry}


def test_txt_exports_thought_after_its_message():
    records = [
        _env(1, "t", {"role": "user", "content": "вопрос", "id": "msg_1"}),
        _thought_env("сперва взвешу\nварианты", "msg_1"),
        _env(2, "t", {"role": "assistant", "content": "ответ", "id": "m2"}),
    ]
    out = export_thread(
        _segs(records),
        fmt="txt",
        api_mode="chat_completions",
        thread_id="tid1",
        system="cli",
    )
    assert (
        "[1] USER\n    вопрос\n\n"
        "[2] THOUGHT\n    сперва взвешу\n    варианты\n\n"
        "[3] ASSISTANT\n    ответ\n"
    ) in out
