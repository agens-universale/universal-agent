"""tail_events: инкремент по offset; stream_reset при смене storage_epoch."""

import json
from pathlib import Path

from gc_tui.services.stores.file_store import FileTaskStore


def _append_record(data_path: Path, seq: int, role: str, text: str) -> None:
    rec = {
        "type": "message",
        "seq": seq,
        "payload": {"id": f"m{seq}", "role": role, "content": text},
    }
    line = json.dumps(rec) + "\n"
    with open(data_path, "a", encoding="utf-8") as f:
        f.write(line)


def _write_meta(
    meta_path: Path,
    *,
    epoch_suffix: str | None = None,
    msgs: int = 0,
    updated: str = "2026-05-20T10:00:00+00:00",
) -> None:
    obj = {
        "thread_id": meta_path.stem.split("_", 2)[-1],
        "updated_at": updated,
        "messages_count": msgs,
    }
    if epoch_suffix is not None:
        obj["last_summary_archive_suffix"] = epoch_suffix
    meta_path.write_text(json.dumps(obj))


def test_read_all_events_returns_records_and_cursor(tmp_path):
    data = tmp_path / "data_cli_t1.jsonl"
    meta = tmp_path / "meta_cli_t1.json"
    _append_record(data, 1, "user", "привет")
    _append_record(data, 2, "assistant", "хай")
    _write_meta(meta, msgs=2)

    store = FileTaskStore(str(tmp_path), system="cli")
    records, cursor = store.read_all_events("t1")
    assert len(records) == 2
    assert records[0]["payload"]["content"] == "привет"
    assert cursor.position_token == data.stat().st_size
    assert cursor.ordering_key == 2


def test_tail_events_returns_only_new_after_cursor(tmp_path):
    data = tmp_path / "data_cli_t1.jsonl"
    meta = tmp_path / "meta_cli_t1.json"
    _append_record(data, 1, "user", "первое")
    _write_meta(meta, msgs=1)

    store = FileTaskStore(str(tmp_path), system="cli")
    _, cur = store.read_all_events("t1")

    _append_record(data, 2, "assistant", "ответ")
    _write_meta(meta, msgs=2)

    tail = store.tail_events("t1", cur)
    assert tail.stream_reset is False
    assert len(tail.new_records) == 1
    assert tail.new_records[0]["payload"]["content"] == "ответ"
    assert tail.next_cursor.ordering_key == 2


def test_tail_events_stream_reset_on_storage_epoch_change(tmp_path):
    data = tmp_path / "data_cli_t1.jsonl"
    meta = tmp_path / "meta_cli_t1.json"
    _append_record(data, 1, "user", "x")
    _write_meta(meta, msgs=1, epoch_suffix="2026-05-20T10-00")

    store = FileTaskStore(str(tmp_path), system="cli")
    _, cur = store.read_all_events("t1")

    _write_meta(meta, msgs=0, epoch_suffix="2026-05-20T11-00")

    tail = store.tail_events("t1", cur)
    assert tail.stream_reset is True


def test_tail_events_stream_reset_on_file_truncation(tmp_path):
    """Файл стал короче прочитанного offset → stream_reset (reason=file truncated)."""
    data = tmp_path / "data_cli_t1.jsonl"
    meta = tmp_path / "meta_cli_t1.json"
    _append_record(data, 1, "user", "длинное первое сообщение")
    _append_record(data, 2, "assistant", "длинный ответ агента")
    _write_meta(meta, msgs=2)

    store = FileTaskStore(str(tmp_path), system="cli")
    _, cur = store.read_all_events("t1")

    # Перезапись файла более коротким содержимым (size < position_token).
    short = {
        "type": "message",
        "seq": 1,
        "payload": {"id": "m1", "role": "user", "content": "x"},
    }
    data.write_text(json.dumps(short) + "\n", encoding="utf-8")
    assert data.stat().st_size < cur.position_token

    tail = store.tail_events("t1", cur)
    assert tail.stream_reset is True
    assert tail.reason == "file truncated"
    assert tail.new_records == []
    assert tail.next_cursor.position_token == 0
    assert tail.next_cursor.ordering_key == 0


def test_tail_events_stream_reset_on_ordering_rollback(tmp_path):
    """Дописана запись с seq меньше максимального виденного → stream_reset."""
    data = tmp_path / "data_cli_t1.jsonl"
    meta = tmp_path / "meta_cli_t1.json"
    _append_record(data, 5, "user", "первое")
    _write_meta(meta, msgs=1)

    store = FileTaskStore(str(tmp_path), system="cli")
    _, cur = store.read_all_events("t1")
    assert cur.ordering_key == 5

    _append_record(data, 3, "assistant", "из прошлого")

    tail = store.tail_events("t1", cur)
    assert tail.stream_reset is True
    assert tail.reason == "not a continuation"
    assert tail.new_records == []
    assert tail.next_cursor.position_token == 0
    assert tail.next_cursor.ordering_key == 0


def test_tail_events_resilient_to_bad_record(tmp_path):
    data = tmp_path / "data_cli_t1.jsonl"
    meta = tmp_path / "meta_cli_t1.json"
    _append_record(data, 1, "user", "valid")
    _write_meta(meta, msgs=1)

    store = FileTaskStore(str(tmp_path), system="cli")
    _, cur = store.read_all_events("t1")

    with open(data, "a", encoding="utf-8") as f:
        f.write("not-a-json\n")
    _append_record(data, 2, "assistant", "after-bad")
    _write_meta(meta, msgs=2)

    tail = store.tail_events("t1", cur)
    payloads = [r["payload"]["content"] for r in tail.new_records]
    assert "after-bad" in payloads


def test_tail_events_idempotent_with_same_cursor(tmp_path):
    data = tmp_path / "data_cli_t1.jsonl"
    meta = tmp_path / "meta_cli_t1.json"
    _append_record(data, 1, "user", "x")
    _write_meta(meta, msgs=1)

    store = FileTaskStore(str(tmp_path), system="cli")
    _, cur = store.read_all_events("t1")

    t1 = store.tail_events("t1", cur)
    t2 = store.tail_events("t1", cur)
    assert t1.new_records == []
    assert t2.new_records == []
    assert t1.next_cursor.position_token == t2.next_cursor.position_token


def test_read_all_events_cursor_counts_archives(tmp_path):
    _append_record(tmp_path / "data_cli_t1.jsonl", 1, "user", "жив")
    for suffix in ("A", "B"):
        arch = tmp_path / f"data_cli_t1__pre_summary__{suffix}.jsonl"
        _append_record(arch, 1, "user", "архив")
    _write_meta(tmp_path / "meta_cli_t1.json", msgs=1)

    store = FileTaskStore(str(tmp_path), system="cli")
    _, cur = store.read_all_events("t1")
    assert cur.archives == 2
    assert cur.wait_ticks == 0


def test_read_all_events_no_archives_zero(tmp_path):
    _append_record(tmp_path / "data_cli_t1.jsonl", 1, "user", "жив")
    _write_meta(tmp_path / "meta_cli_t1.json", msgs=1)

    store = FileTaskStore(str(tmp_path), system="cli")
    _, cur = store.read_all_events("t1")
    assert cur.archives == 0


def test_read_all_events_stops_before_partial_line(tmp_path):
    """Недописанная последняя строка не уходит за курсор: дописанная, она
    придёт следующим тиком."""
    data = tmp_path / "data_cli_t1.jsonl"
    _append_record(data, 1, "user", "целая")
    _write_meta(tmp_path / "meta_cli_t1.json", msgs=2)
    whole = data.stat().st_size
    line = json.dumps(
        {
            "type": "message",
            "seq": 2,
            "payload": {"id": "m2", "role": "user", "content": "вторая"},
        }
    )
    with open(data, "a", encoding="utf-8") as f:
        f.write(line[:10])

    store = FileTaskStore(str(tmp_path), system="cli")
    records, cur = store.read_all_events("t1")
    with open(data, "a", encoding="utf-8") as f:
        f.write(line[10:] + "\n")
    tail = store.tail_events("t1", cur)

    assert [r["payload"]["id"] for r in records] == ["m1"]
    assert cur.position_token == whole
    assert [r["payload"]["id"] for r in tail.new_records] == ["m2"]


def test_tail_events_waits_for_partial_line(tmp_path):
    """Тик посреди записи строки: курсор встаёт до обрывка, и дописанная
    запись приходит следующим тиком, а не теряется."""
    data = tmp_path / "data_cli_t1.jsonl"
    _append_record(data, 1, "user", "целая")
    _write_meta(tmp_path / "meta_cli_t1.json", msgs=2)
    store = FileTaskStore(str(tmp_path), system="cli")
    _, cur = store.read_all_events("t1")
    line = json.dumps(
        {
            "type": "message",
            "seq": 2,
            "payload": {"id": "m2", "role": "user", "content": "вторая"},
        }
    )
    with open(data, "a", encoding="utf-8") as f:
        f.write(line[:10])

    first = store.tail_events("t1", cur)
    with open(data, "a", encoding="utf-8") as f:
        f.write(line[10:] + "\n")
    second = store.tail_events("t1", first.next_cursor)

    assert first.new_records == []
    assert first.next_cursor.position_token == cur.position_token
    assert [r["payload"]["id"] for r in second.new_records] == ["m2"]
