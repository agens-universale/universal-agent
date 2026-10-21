"""Удаление треда (``FileThreadStore.delete``) убирает его файлы во всех
формах, не трогая соседей, — включая лок журнала правок рядом с журналом;
занятый или неудаляемый лок удаление не срывает."""

import asyncio
import json
from pathlib import Path

from gc_core.thread_store import edits_journal_path, load_thread_messages
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey

KEY = ThreadKey(system="cli", thread="demo-thread")


def _delete(threads_dir: Path, key: ThreadKey = KEY) -> None:
    asyncio.run(FileThreadStore(threads_dir).delete(key, op_id="delete"))


def test_delete_removes_only_target_thread_files(tmp_path: Path) -> None:
    threads_dir = tmp_path / "threads"
    (threads_dir / "locks").mkdir(parents=True)
    target_data = threads_dir / "data_cli_demo-thread.jsonl"
    target_meta = threads_dir / "meta_cli_demo-thread.json"
    target_lock = threads_dir / "locks" / "lock_cli_demo-thread.json"
    other_data = threads_dir / "data_cli_other-thread.jsonl"
    other_meta = threads_dir / "meta_cli_other-thread.json"
    target_data.write_text("", encoding="utf-8")
    target_meta.write_text("{}", encoding="utf-8")
    target_lock.write_text("{}", encoding="utf-8")
    other_data.write_text("", encoding="utf-8")
    other_meta.write_text("{}", encoding="utf-8")

    _delete(threads_dir)

    assert not target_data.exists()
    assert not target_meta.exists()
    assert not target_lock.exists()
    assert other_data.exists()
    assert other_meta.exists()


def test_delete_removes_edits_journal_and_its_lock(tmp_path: Path) -> None:
    """``file_lock`` кладёт межпроцессный лок журнала правок рядом как
    ``<журнал>.lock`` и не удаляет его сам. Удаление треда сносит журнал и
    обязано снести лок: иначе осиротевший ``.lock`` остаётся навсегда."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    (threads_dir / "data_cli_demo-thread.jsonl").write_text(
        "", encoding="utf-8"
    )
    journal = edits_journal_path(str(threads_dir), "cli", "demo-thread")
    journal.write_text("", encoding="utf-8")
    journal_lock = Path(str(journal) + ".lock")
    journal_lock.write_bytes(b"")

    _delete(threads_dir)

    assert not journal.exists()
    assert not journal_lock.exists()


def test_delete_removes_legacy_thread_files(tmp_path: Path) -> None:
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    legacy_data = threads_dir / "data_legacy-1.jsonl"
    legacy_meta = threads_dir / "meta_legacy-1.json"
    legacy_data.write_text("", encoding="utf-8")
    legacy_meta.write_text("{}", encoding="utf-8")

    _delete(threads_dir, ThreadKey(system="cli", thread="legacy-1"))

    assert not legacy_data.exists()
    assert not legacy_meta.exists()


def test_load_thread_messages_accepts_double_encoded_jsonl_rows(
    tmp_path: Path,
) -> None:
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
    line = (
        json.dumps(json.dumps(record, ensure_ascii=False), ensure_ascii=False)
        + "\n"
    )
    (threads_dir / "data_cli_t1.jsonl").write_text(line, encoding="utf-8")

    messages, keys, max_seq = load_thread_messages(
        str(threads_dir), "cli", "t1"
    )

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert max_seq == 1


def test_delete_survives_a_lock_held_by_another_process(
    tmp_path: Path,
) -> None:
    """Захваченный лок журнала не срывает удаление треда."""
    from gc_core.utils.file_lock import file_lock

    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    journal = edits_journal_path(str(threads_dir), "cli", "demo-thread")
    journal.write_text("", encoding="utf-8")
    data = threads_dir / "data_cli_demo-thread.jsonl"
    data.write_text("", encoding="utf-8")

    with file_lock(journal, timeout=5.0):
        _delete(threads_dir)

    assert not data.exists()


def test_delete_keeps_going_when_the_lock_refuses_to_die(
    tmp_path: Path, monkeypatch
) -> None:
    """Отказ удаления лока журнала (файл занят — Windows) не срывает
    удаление треда: лок остаётся, остальные файлы — удалены."""
    import os as _os

    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    journal = edits_journal_path(str(threads_dir), "cli", "demo-thread")
    journal.write_text("", encoding="utf-8")
    journal_lock = Path(str(journal) + ".lock")
    journal_lock.write_bytes(b"")
    data = threads_dir / "data_cli_demo-thread.jsonl"
    data.write_text("", encoding="utf-8")

    real_unlink = _os.unlink

    def stubborn_unlink(path, *args, **kwargs):
        if str(path) == str(journal_lock):
            raise PermissionError(22, "файл занят другим процессом")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(_os, "unlink", stubborn_unlink)
    _delete(threads_dir)

    assert not data.exists()
    assert not journal.exists()
    assert journal_lock.exists()
