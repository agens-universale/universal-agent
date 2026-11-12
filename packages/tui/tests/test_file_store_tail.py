"""tail_events: только новое после курсора; stream_reset при сжатии
истории и при пересоздании треда."""

import asyncio
import json
from pathlib import Path

from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_testkit.thread_writes import acompact
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
    assert cursor.ordering_key == 2
    assert store.tail_events("t1", cursor).new_records == []


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


def test_tail_events_stream_reset_after_compaction(tmp_path):
    store = FileTaskStore(str(tmp_path), system="cli")
    store.append_messages("t1", [{"id": "m1", "role": "user", "content": "x"}])
    _, cur = store.read_all_events("t1")

    summary = {"id": "s", "role": "user", "content": "сводка"}
    asyncio.run(
        acompact(
            FileThreadStore(tmp_path),
            ThreadKey(system="cli", thread="t1"),
            [summary],
        )
    )

    tail = store.tail_events("t1", cur)
    assert tail.stream_reset is True
    assert tail.new_records == []
    records, _cur = store.read_all_events("t1")
    assert [r["payload"]["id"] for r in records] == ["s"]


def test_tail_events_stream_reset_when_thread_is_recreated(tmp_path):
    store = FileTaskStore(str(tmp_path), system="cli")
    store.append_messages("t1", [{"id": "m1", "role": "user", "content": "a"}])
    _, cur = store.read_all_events("t1")
    store.delete_task("t1")
    store.append_messages("t1", [{"id": "n1", "role": "user", "content": "b"}])

    tail = store.tail_events("t1", cur)
    assert tail.stream_reset is True
    assert tail.reason == "thread recreated"


def test_thread_created_after_attach_is_read_from_the_start(tmp_path):
    """Треда при attach ещё нет (новая сессия): появился — его записи
    приходят хвостом, без сброса показа."""
    store = FileTaskStore(str(tmp_path), system="cli")
    records, cur = store.read_all_events("t1")
    assert records == []
    assert store.tail_events("t1", cur).new_records == []

    store.append_messages("t1", [{"id": "m1", "role": "user", "content": "a"}])
    tail = store.tail_events("t1", cur)
    assert tail.stream_reset is False
    assert [r["payload"]["id"] for r in tail.new_records] == ["m1"]
    again = store.tail_events("t1", tail.next_cursor)
    assert again.new_records == []


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
    assert t1.next_cursor == t2.next_cursor


def test_tail_tick_survives_a_lock_timeout(tmp_path, monkeypatch):
    """Блокировку треда держит агент, а ``msvcrt.locking`` на Windows после
    ~10 с ждёт уже ``OSError``: тик пропускается, курсор на месте — следующий
    дочитает; опрос UI не падает."""
    data = tmp_path / "data_cli_t1.jsonl"
    _append_record(data, 1, "user", "первое")
    _write_meta(tmp_path / "meta_cli_t1.json", msgs=1)
    store = FileTaskStore(str(tmp_path), system="cli")
    _, cur = store.read_all_events("t1")
    _append_record(data, 2, "assistant", "ответ")

    def locked(*_args, **_kwargs):
        raise OSError(36, "Resource deadlock avoided")

    with monkeypatch.context() as patched:
        patched.setattr(store._threads, "tail", locked)
        tail = store.tail_events("t1", cur)
    assert tail.new_records == [] and tail.next_cursor == cur
    assert len(store.tail_events("t1", cur).new_records) == 1


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
    assert cur.ordering_key == 1
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
    assert first.next_cursor == cur
    assert [r["payload"]["id"] for r in second.new_records] == ["m2"]
