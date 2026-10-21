"""read_segments: тред (архивы + live) → сегменты с синтезом сжатий."""

import json

from gc_tui.services.stores.file_store import FileTaskStore

_TID = "seg00000000000000000000000000000a"


def _sum_msg(text):
    return {
        "role": "user",
        "content": f'<technical type="dialog_summary">Summarized context from previous dialogue.\n<summary>{text}</summary></technical>',
        "id": "m_s",
    }


def _write_file(d, name, records):
    with open(d / name, "w", encoding="utf-8") as f:
        for i, p in enumerate(records, 1):
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "thread_id": _TID,
                        "system": "cli",
                        "seq": i,
                        "ts": f"2026-06-03T10:0{i}:00+00:00",
                        "payload": p,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def test_segments_two_compactions(tmp_path):
    # archive1 = [Seg0 raw];  archive2 = [summary1, Seg1 raw];  live = [summary2, Seg2 raw]
    _write_file(
        tmp_path,
        f"data_cli_{_TID}__pre_summary__20260603_100000.jsonl",
        [{"role": "user", "content": "вопрос-0"}],
    )
    _write_file(
        tmp_path,
        f"data_cli_{_TID}__pre_summary__20260603_110000.jsonl",
        [_sum_msg("СИНТЕЗ-1"), {"role": "user", "content": "вопрос-1"}],
    )
    _write_file(
        tmp_path,
        f"data_cli_{_TID}.jsonl",
        [_sum_msg("СИНТЕЗ-2"), {"role": "user", "content": "вопрос-2"}],
    )

    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    segs = store.read_segments(_TID)

    assert len(segs) == 3
    # seg0: raw "вопрос-0", сжат → СИНТЕЗ-1
    assert segs[0]["messages"][0]["payload"]["content"] == "вопрос-0"
    assert segs[0]["compacted_by"]["synthesis"] == "СИНТЕЗ-1"
    assert segs[0]["compacted_by"]["messages_in_segment"] == 1
    # seg1: raw "вопрос-1", сжат → СИНТЕЗ-2
    assert segs[1]["messages"][0]["payload"]["content"] == "вопрос-1"
    assert segs[1]["compacted_by"]["synthesis"] == "СИНТЕЗ-2"
    # seg2 (live): raw "вопрос-2", не сжат
    assert segs[2]["messages"][0]["payload"]["content"] == "вопрос-2"
    assert segs[2]["compacted_by"] is None


def test_segments_no_compaction(tmp_path):
    _write_file(
        tmp_path,
        f"data_cli_{_TID}.jsonl",
        [
            {"role": "user", "content": "привет"},
            {"role": "assistant", "content": "ответ"},
        ],
    )
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    segs = store.read_segments(_TID)
    assert len(segs) == 1
    assert segs[0]["compacted_by"] is None
    assert len(segs[0]["messages"]) == 2


def test_segments_expose_chars_before_after(tmp_path):
    # seg0 raw = 10 символов; сжат → synthesis "abcde" = 5 символов
    _write_file(
        tmp_path,
        f"data_cli_{_TID}__pre_summary__20260603_100000.jsonl",
        [{"role": "user", "content": "0123456789"}],
    )
    _write_file(
        tmp_path,
        f"data_cli_{_TID}.jsonl",
        [_sum_msg("abcde"), {"role": "user", "content": "вопрос-2"}],
    )

    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    segs = store.read_segments(_TID)

    assert segs[0]["compacted_by"]["chars_before"] == 10
    assert segs[0]["compacted_by"]["chars_after"] == 5
    # live-сегмент без сжатия — полей нет (compacted_by is None)
    assert segs[1]["compacted_by"] is None


def test_chars_before_is_trigger_measure(tmp_path):
    """chars_before — мера триггера ядра: с аргументами вызовов и выводом
    тулов, а не только `content` реплик (спека P3 §2.5)."""
    from gc_core.message_format.serialization import (
        calc_history_chars_for_trigger,
    )

    seg = [
        {"role": "user", "content": "прочитай файл"},
        {
            "type": "function_call",
            "name": "fs_read_file",
            "call_id": "c1",
            "arguments": json.dumps({"path": "client.py"}),
        },
        {"type": "function_call_output", "call_id": "c1", "output": "x" * 500},
        {"role": "assistant", "content": "прочитал"},
    ]
    _write_file(
        tmp_path, f"data_cli_{_TID}__pre_summary__20260603_100000.jsonl", seg
    )
    _write_file(
        tmp_path,
        f"data_cli_{_TID}.jsonl",
        [_sum_msg("abcde"), {"role": "user", "content": "вопрос-2"}],
    )

    segs = FileTaskStore(
        threads_dir=str(tmp_path), system="cli"
    ).read_segments(_TID)

    expected = calc_history_chars_for_trigger(seg)
    assert expected > 500 + len("прочитай файл")
    assert segs[0]["compacted_by"]["chars_before"] == expected
