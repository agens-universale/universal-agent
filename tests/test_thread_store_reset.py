import json
from pathlib import Path

from thread_store import load_thread_messages, reset_thread_files


def test_reset_thread_files_deletes_only_target_thread_files(tmp_path: Path) -> None:
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()

    target_data = threads_dir / "data_cli_demo-thread.jsonl"
    target_meta = threads_dir / "meta_cli_demo-thread.json"
    target_lock = threads_dir / "locks" / "lock_cli_demo-thread.json"
    other_data = threads_dir / "data_cli_other-thread.jsonl"
    other_meta = threads_dir / "meta_cli_other-thread.json"

    (threads_dir / "locks").mkdir()
    target_data.write_text("x\n", encoding="utf-8")
    target_meta.write_text("{}", encoding="utf-8")
    target_lock.write_text("{}", encoding="utf-8")
    other_data.write_text("y\n", encoding="utf-8")
    other_meta.write_text("{}", encoding="utf-8")

    deleted = reset_thread_files(str(threads_dir), system_id="cli", thread_id="demo-thread")

    assert str(target_data) in deleted
    assert str(target_meta) in deleted
    assert str(target_lock) in deleted
    assert not target_data.exists()
    assert not target_meta.exists()
    assert not target_lock.exists()
    assert other_data.exists()
    assert other_meta.exists()


def test_reset_thread_files_deletes_legacy_thread_files(tmp_path: Path) -> None:
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()

    legacy_data = threads_dir / "data_legacy-1.jsonl"
    legacy_meta = threads_dir / "meta_legacy-1.json"
    legacy_data.write_text("x\n", encoding="utf-8")
    legacy_meta.write_text("{}", encoding="utf-8")

    deleted = reset_thread_files(str(threads_dir), system_id="cli", thread_id="legacy-1")

    assert str(legacy_data) in deleted
    assert str(legacy_meta) in deleted
    assert not legacy_data.exists()
    assert not legacy_meta.exists()


def test_load_thread_messages_accepts_double_encoded_jsonl_rows(tmp_path: Path) -> None:
    """Строка как json.dumps(json.dumps(record)) — нештатно, но загружаем без падения."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    record = {
        "type": "message",
        "thread_id": "t1",
        "system": "cli",
        "seq": 1,
        "ts": "2026-01-01T00:00:00+00:00",
        "payload": {"role": "user", "content": "hi", "id": "m1"},
    }
    line = json.dumps(json.dumps(record, ensure_ascii=False), ensure_ascii=False) + "\n"
    (threads_dir / "data_cli_t1.jsonl").write_text(line, encoding="utf-8")

    messages, keys, max_seq = load_thread_messages(str(threads_dir), "cli", "t1")

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert max_seq == 1
